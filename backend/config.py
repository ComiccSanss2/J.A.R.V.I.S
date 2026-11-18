"""Central configuration for J.A.R.V.I.S. OS (read from .env / environment variables)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    # --- Server ---
    host: str = field(default_factory=lambda: _str("JARVIS_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_str("JARVIS_PORT", "8765")))
    ssl: bool = field(default_factory=lambda: _bool("JARVIS_SSL", False))
    access_token: str = field(default_factory=lambda: _str("JARVIS_ACCESS_TOKEN"))
    data_dir: Path = field(default_factory=lambda: ROOT_DIR / _str("JARVIS_DATA_DIR", "data"))

    # --- Claude ---
    anthropic_api_key: str = field(default_factory=lambda: _str("ANTHROPIC_API_KEY"))
    model: str = field(default_factory=lambda: _str("JARVIS_MODEL", "claude-haiku-5-5"))
    effort: str = field(default_factory=lambda: _str("JARVIS_EFFORT", "low"))
    max_tokens: int = field(default_factory=lambda: int(_str("JARVIS_MAX_TOKENS", "16000")))
    history_limit: int = field(default_factory=lambda: int(_str("JARVIS_HISTORY_LIMIT", "40")))
    enable_web_search: bool = field(default_factory=lambda: _bool("JARVIS_WEB_SEARCH", True))

    # --- Web browsing (J.A.R.V.I.S. drives his own Edge/Chrome window) ---
    web_browsing: bool = field(default_factory=lambda: _bool("JARVIS_WEB_BROWSING", True))
    # msedge | chrome | empty = try Edge, then Chrome
    web_browser_channel: str = field(default_factory=lambda: _str("JARVIS_WEB_BROWSER"))

    # --- Music ---
    # app = open YouTube Music in its installed desktop app when there is one; browser = always the browser
    music_player: str = field(default_factory=lambda: _str("JARVIS_MUSIC_PLAYER", "app").lower())
    # false = start the music minimized in the background (no window in front of you); true = bring it to the front
    music_foreground: bool = field(default_factory=lambda: _bool("JARVIS_MUSIC_FOREGROUND", False))
    # optional: path of the YouTube Music .lnk shortcut, if it is not found automatically
    ytmusic_shortcut: str = field(default_factory=lambda: _str("JARVIS_YTMUSIC_SHORTCUT"))

    # --- Wake word & interruption ---
    # true = the mic is always on but J.A.R.V.I.S. only reacts to phrases starting with "Hey Jarvis"
    wake_word: bool = field(default_factory=lambda: _bool("JARVIS_WAKE_WORD", True))
    # seconds after an answer during which you can follow up without saying "Hey Jarvis" (0 = never)
    followup_seconds: int = field(default_factory=lambda: int(_str("JARVIS_FOLLOWUP_SECONDS", "0")))
    # true = keep listening while J.A.R.V.I.S. talks, so "Hey Jarvis, ..." interrupts him
    barge_in: bool = field(default_factory=lambda: _bool("JARVIS_BARGE_IN", True))

    # --- Developer tools (create files, write code, run terminal commands) ---
    dev_tools: bool = field(default_factory=lambda: _bool("JARVIS_DEV_TOOLS", True))
    workspace: Path = field(default_factory=lambda: Path(_str("JARVIS_WORKSPACE", str(Path.home() / "JARVIS-Workspace"))).expanduser())
    confirm_commands: bool = field(default_factory=lambda: _bool("JARVIS_CONFIRM_COMMANDS", True))
    # How J.A.R.V.I.S. addresses you in each language mode
    user_title_en: str = field(default_factory=lambda: _str("JARVIS_USER_TITLE", "Sir"))
    user_title_it: str = field(default_factory=lambda: _str("JARVIS_USER_TITLE_IT", "Signore"))
    user_title_fr: str = field(default_factory=lambda: _str("JARVIS_USER_TITLE_FR", "Monsieur"))

    # --- Locale ---
    # Default language mode at first start: en | it | fr (then switchable from the HUD)
    language: str = field(default_factory=lambda: _str("JARVIS_LANGUAGE", "en"))
    timezone: str = field(default_factory=lambda: _str("JARVIS_TIMEZONE", "Europe/Rome"))
    default_city: str = field(default_factory=lambda: _str("JARVIS_CITY", "Rome"))

    # --- Voice ---
    tts_provider: str = field(default_factory=lambda: _str("TTS_PROVIDER", "auto").lower())
    stt_provider: str = field(default_factory=lambda: _str("STT_PROVIDER", "auto").lower())
    elevenlabs_api_key: str = field(default_factory=lambda: _str("ELEVENLABS_API_KEY"))
    elevenlabs_voice_id: str = field(default_factory=lambda: _str("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb"))
    elevenlabs_model: str = field(default_factory=lambda: _str("ELEVENLABS_MODEL", "eleven_flash_v2_5"))
    elevenlabs_stability: float = field(default_factory=lambda: _float("ELEVENLABS_STABILITY", 0.45))
    elevenlabs_similarity: float = field(default_factory=lambda: _float("ELEVENLABS_SIMILARITY", 0.8))
    elevenlabs_style: float = field(default_factory=lambda: _float("ELEVENLABS_STYLE", 0.2))
    elevenlabs_speed: float = field(default_factory=lambda: _float("ELEVENLABS_SPEED", 1.0))
    openai_api_key: str = field(default_factory=lambda: _str("OPENAI_API_KEY"))
    openai_tts_model: str = field(default_factory=lambda: _str("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"))
    openai_tts_voice: str = field(default_factory=lambda: _str("OPENAI_TTS_VOICE", "ash"))
    openai_tts_instructions: str = field(default_factory=lambda: _str(
        "OPENAI_TTS_INSTRUCTIONS",
        "Refined British butler. Calm, warm, quietly confident, with dry understated wit. "
        "Natural pace, light pauses between sentences, never theatrical."))
    openai_stt_model: str = field(default_factory=lambda: _str("OPENAI_STT_MODEL", "whisper-1"))
    deepgram_api_key: str = field(default_factory=lambda: _str("DEEPGRAM_API_KEY"))
    edge_tts_voice: str = field(default_factory=lambda: _str("EDGE_TTS_VOICE", "en-GB-RyanNeural"))
    edge_tts_rate: str = field(default_factory=lambda: _str("EDGE_TTS_RATE", "-4%"))
    edge_tts_pitch: str = field(default_factory=lambda: _str("EDGE_TTS_PITCH", "-3Hz"))
    edge_tts_volume: str = field(default_factory=lambda: _str("EDGE_TTS_VOLUME", "+0%"))
    edge_tts_voice_it: str = field(default_factory=lambda: _str("EDGE_TTS_VOICE_IT", "it-IT-DiegoNeural"))
    edge_tts_rate_it: str = field(default_factory=lambda: _str("EDGE_TTS_RATE_IT", "+0%"))
    edge_tts_pitch_it: str = field(default_factory=lambda: _str("EDGE_TTS_PITCH_IT", "-2Hz"))
    edge_tts_voice_fr: str = field(default_factory=lambda: _str("EDGE_TTS_VOICE_FR", "fr-FR-HenriNeural"))
    edge_tts_rate_fr: str = field(default_factory=lambda: _str("EDGE_TTS_RATE_FR", "+0%"))
    edge_tts_pitch_fr: str = field(default_factory=lambda: _str("EDGE_TTS_PITCH_FR", "-2Hz"))
    local_whisper_model: str = field(default_factory=lambda: _str("LOCAL_WHISPER_MODEL", "small"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jarvis.db"

    @property
    def config_dir(self) -> Path:
        return ROOT_DIR / "config"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
