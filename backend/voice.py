"""Voice: speech synthesis (TTS) and transcription (STT) with a fallback chain.

TTS:  ElevenLabs -> OpenAI TTS -> edge-tts (free) -> (client) Web Speech API
STT:  OpenAI Whisper | Deepgram | local faster-whisper | (client) Web Speech API

Every tone setting (voice, speed, pitch, expressiveness) lives in .env.
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from .config import Settings
from .i18n import lang

log = logging.getLogger("jarvis.voice")

# Phrases Whisper tends to "hear" in silence
HALLUCINATIONS = {
    "thank you for watching", "thanks for watching", "thank you", "you", "bye", ".",
    "sottotitoli creati dalla comunità amara.org", "grazie per la visione",
}


def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)      # markdown links
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`>|~]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ======================= TTS =======================
class TTSError(RuntimeError):
    pass


def tts_chain(s: Settings) -> list[str]:
    if s.tts_provider == "browser":
        return []
    if s.tts_provider != "auto":
        return [s.tts_provider]
    chain = []
    if s.elevenlabs_api_key:
        chain.append("elevenlabs")
    if s.openai_api_key:
        chain.append("openai")
    chain.append("edge")
    return chain


async def _tts_elevenlabs(text: str, s: Settings) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{s.elevenlabs_voice_id}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            url, params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": s.elevenlabs_api_key, "accept": "audio/mpeg"},
            json={
                "text": text, "model_id": s.elevenlabs_model, "language_code": lang.code,
                "voice_settings": {
                    "stability": s.elevenlabs_stability,
                    "similarity_boost": s.elevenlabs_similarity,
                    "style": s.elevenlabs_style,
                    "speed": s.elevenlabs_speed,
                    "use_speaker_boost": True,
                },
            },
        )
    if r.status_code != 200:
        raise TTSError(f"ElevenLabs {r.status_code}: {r.text[:200]}")
    return r.content


async def _tts_openai(text: str, s: Settings) -> bytes:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {s.openai_api_key}"},
            json={"model": s.openai_tts_model, "voice": s.openai_tts_voice, "input": text,
                  "response_format": "mp3", "instructions": s.openai_tts_instructions},
        )
    if r.status_code != 200:
        raise TTSError(f"OpenAI TTS {r.status_code}: {r.text[:200]}")
    return r.content


async def _tts_edge(text: str, s: Settings) -> bytes:
    import edge_tts

    p = lang.profile  # voice, speed and pitch follow the active language mode
    comm = edge_tts.Communicate(text, p.edge_voice, rate=p.edge_rate,
                                pitch=p.edge_pitch, volume=s.edge_tts_volume)
    audio = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
    if not audio:
        raise TTSError("edge-tts returned no audio")
    return bytes(audio)


TTS_PROVIDERS = {"elevenlabs": _tts_elevenlabs, "openai": _tts_openai, "edge": _tts_edge}


async def synthesize(text: str, s: Settings) -> tuple[bytes, str]:
    text = clean_for_speech(text)[:2500]
    if not text:
        raise TTSError("Empty text")
    errors = []
    for name in tts_chain(s):
        fn = TTS_PROVIDERS.get(name)
        if not fn:
            continue
        try:
            return await fn(text, s), name
        except Exception as exc:
            log.warning("TTS %s failed: %s", name, exc)
            errors.append(f"{name}: {exc}")
    raise TTSError("; ".join(errors) or "No server TTS provider configured")


# ======================= STT =======================
def local_whisper_installed() -> bool:
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None


def stt_fallback_provider(s: Settings) -> str | None:
    """Server STT a browser can switch to when its own recognition fails (e.g. Brave)."""
    if s.stt_provider == "browser":
        return None
    return stt_server_provider(s) or ("local" if local_whisper_installed() else None)


def stt_server_provider(s: Settings) -> str | None:
    """Server-side STT provider to use, or None (=> Web Speech API in the browser)."""
    p = s.stt_provider
    if p == "browser":
        return None
    if p in {"openai", "deepgram", "local"}:
        return p
    if s.deepgram_api_key:
        return "deepgram"
    if s.openai_api_key:
        return "openai"
    return None


async def _stt_openai(audio: bytes, s: Settings) -> str:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {s.openai_api_key}"},
            data={"model": s.openai_stt_model, "language": lang.code, "response_format": "json"},
            files={"file": ("speech.wav", audio, "audio/wav")},
        )
    r.raise_for_status()
    return r.json().get("text", "")


async def _stt_deepgram(audio: bytes, s: Settings) -> str:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            "https://api.deepgram.com/v1/listen",
            params={"model": "nova-3", "language": lang.code, "smart_format": "true"},
            headers={"Authorization": f"Token {s.deepgram_api_key}", "Content-Type": "audio/wav"},
            content=audio,
        )
    r.raise_for_status()
    alts = r.json()["results"]["channels"][0]["alternatives"]
    return alts[0]["transcript"] if alts else ""


_local_model = None


async def _stt_local(audio: bytes, s: Settings) -> str:
    """Offline transcription with faster-whisper (optional: pip install faster-whisper)."""
    import io

    def run() -> str:
        global _local_model
        from faster_whisper import WhisperModel

        if _local_model is None:
            _local_model = WhisperModel(s.local_whisper_model, device="auto", compute_type="int8")
        segments, _ = _local_model.transcribe(io.BytesIO(audio), language=lang.code, vad_filter=True)
        return " ".join(seg.text for seg in segments)

    return await asyncio.to_thread(run)


STT_PROVIDERS = {"openai": _stt_openai, "deepgram": _stt_deepgram, "local": _stt_local}


async def transcribe(audio: bytes, s: Settings) -> str:
    provider = stt_fallback_provider(s)
    if not provider:
        raise RuntimeError("No server STT provider configured")
    text = (await STT_PROVIDERS[provider](audio, s)).strip()
    if text.lower().strip(" .!") in {h.strip(" .!") for h in HALLUCINATIONS}:
        return ""
    return text
