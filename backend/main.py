"""J.A.R.V.I.S. OS - central server (FastAPI + WebSocket)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import voice
from .brain import Brain
from .config import ROOT_DIR, settings
from .hub import Client, Hub
from .i18n import PROFILES, lang
from .location import reverse_geocode, valid_coords, valid_timezone
from .memory import Memory
from .skills import SkillRegistry
from .skills.weather import fetch_forecast, fetch_weather

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("jarvis")

FRONTEND_DIR = ROOT_DIR / "frontend"
SESSION_RE = re.compile(r"^[\w\-]{1,48}$")
MAX_AUDIO_BYTES = 15 * 1024 * 1024
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
# Bumped whenever the client/server protocol changes: the HUD warns if the server is outdated
API_VERSION = 4

memory = Memory(settings.db_path)
# The answer currently being generated in each session: a new request (or an interruption) cancels it
running_turns: dict[str, asyncio.Task] = {}


def cancel_turn(session_id: str) -> bool:
    task = running_turns.get(session_id)
    if task and not task.done():
        task.cancel()
        return True
    return False
hub = Hub(memory)
registry = SkillRegistry(settings)
brain = Brain(settings, memory, registry, hub)
lang.set(memory.get_pref("language") or settings.language)  # last mode chosen from the HUD
_fg = memory.get_pref("music_foreground")                    # music window option chosen from the HUD
if _fg is not None:
    settings.music_foreground = _fg == "1"


def public_prefs() -> dict:
    """Options changeable from the HUD settings panel (shared by every device)."""
    return {"music_foreground": settings.music_foreground}


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("J.A.R.V.I.S. OS online - model %s, effort %s, language %s", settings.model, settings.effort, lang.code)
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY not set: falling back to 'ant auth login' credentials, if any.")
    yield


app = FastAPI(title="J.A.R.V.I.S. OS", lifespan=lifespan)


# ---------------- authentication (shared token, optional) ----------------
def _token_ok(token: str | None) -> bool:
    if not settings.access_token:
        return True
    return bool(token) and secrets.compare_digest(token, settings.access_token)


def require_token(request: Request) -> None:
    token = request.headers.get("x-jarvis-token") or request.query_params.get("token")
    if not _token_ok(token):
        raise HTTPException(status_code=401, detail="Invalid access token")


# ---------------- API HTTP ----------------
@app.get("/api/health")
async def health():
    return {"status": "online", "clients": len(hub.clients)}


@app.get("/api/config")
async def client_config(request: Request):
    require_token(request)
    return {
        "api_version": API_VERSION,
        "prefs": public_prefs(),
        "language": lang.profile.public(),
        "languages": list(PROFILES),
        "default_city": settings.default_city,
        "timezone": settings.timezone,
        "model": settings.model,
        "stt_server": voice.stt_server_provider(settings),
        "stt_fallback": voice.stt_fallback_provider(settings),
        "wake_word": settings.wake_word,
        "followup_seconds": max(0, settings.followup_seconds),
        "barge_in": settings.barge_in,
        "tts_chain": voice.tts_chain(settings),
        "skills": registry.catalog(),
    }


@app.get("/api/weather")
async def weather(request: Request, city: str | None = Query(default=None, max_length=80),
                  lat: float | None = None, lon: float | None = None):
    require_token(request)
    try:
        coords = valid_coords(lat, lon) if lat is not None and lon is not None else None
        if coords and not city:  # real position of the requesting device
            place = await reverse_geocode(*coords, lang.code)
            return {**(await fetch_forecast(place, 3, lang.code)), "source": "device"}
        return {**(await fetch_weather(city or settings.default_city, 3, lang.code)), "source": "default"}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/prefs")
async def set_prefs(request: Request):
    """Changes HUD options (for now: whether the YouTube Music window comes to the front)."""
    require_token(request)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Body must be JSON")
    if "music_foreground" in body:
        settings.music_foreground = bool(body["music_foreground"])
        await memory.set_pref("music_foreground", "1" if settings.music_foreground else "0")
    prefs = public_prefs()
    await hub.broadcast({"type": "prefs", "prefs": prefs})
    return {"prefs": prefs}


# ---------------- helper agents (e.g. mac_agent.js on the MacBook) ----------------
@app.get("/api/agent/poll")
async def agent_poll(request: Request, name: str = Query(max_length=40), platform: str = Query(default="unknown", max_length=20)):
    """The agent waits here for the next job (long polling: up to 25 s, then 204 and it asks again)."""
    require_token(request)
    job = await hub.agent_poll(_clean(name, "DEVICE").upper(), platform)
    return job if job else Response(status_code=204)


@app.post("/api/agent/result")
async def agent_result(request: Request):
    require_token(request)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Body must be JSON")
    job_id = str(body.get("job_id", ""))
    if not job_id.startswith("agent:"):
        raise HTTPException(400, "Unknown job")
    hub.resolve_client_tool(job_id, body.get("result"))
    return {"ok": True}


@app.post("/api/language")
async def set_language(request: Request):
    """Switches English / Italian mode for every connected device."""
    require_token(request)
    body = await request.json()
    code = str(body.get("language", "")).lower()
    if code not in PROFILES:
        raise HTTPException(400, f"Unsupported language. Available: {list(PROFILES)}")
    profile = lang.set(code)
    await memory.set_pref("language", code)
    log.info("Language mode -> %s", code)
    await hub.broadcast({"type": "language", "language": profile.public()})
    return {"language": profile.public()}


@app.post("/api/tts")
async def tts(request: Request):
    require_token(request)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Body must be UTF-8 JSON: {\"text\": \"...\"}")
    text = str(body.get("text", ""))[:4000]
    try:
        audio, provider = await voice.synthesize(text, settings)
    except voice.TTSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return Response(audio, media_type="audio/mpeg", headers={"X-TTS-Provider": provider, "Cache-Control": "no-store"})


@app.post("/api/stt")
async def stt(request: Request):
    require_token(request)
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "Missing audio")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Audio too long")
    try:
        text = await voice.transcribe(audio, settings)
    except Exception as exc:
        log.warning("STT failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=503)
    return {"text": text}


# ---------------- WebSocket: sync channel ----------------
def _clean(value: str | None, default: str, limit: int = 40) -> str:
    value = (value or "").strip()
    return value[:limit] if value else default


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    q = ws.query_params
    if not _token_ok(q.get("token")):
        await ws.close(code=4401)
        return
    session_id = q.get("session") or "default"
    if not SESSION_RE.match(session_id):
        await ws.close(code=4400)
        return
    await ws.accept()

    host = ws.client.host if ws.client else ""
    client = Client(
        ws=ws,
        client_id=uuid.uuid4().hex[:12],
        device_id=_clean(q.get("device_id"), uuid.uuid4().hex, 64),
        device_name=_clean(q.get("device_name"), "DEVICE").upper(),
        platform=_clean(q.get("platform"), "unknown"),
        agent="electron" if q.get("agent") == "electron" else "browser",
        session_id=session_id,
        is_local=host in LOCAL_HOSTS,
        timezone=valid_timezone(q.get("tz")),
    )
    await hub.register(client)
    await hub.send(client.client_id, {
        "type": "hello",
        "client_id": client.client_id,
        "session": session_id,
        "history": await memory.history(session_id, 100),
        "devices": await hub.devices_snapshot(),
        "facts": await memory.facts(),
        "skills": registry.catalog(),
        "language": lang.profile.public(),
    })

    tasks: set[asyncio.Task] = set()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "user_message":
                text = str(msg.get("text", ""))[:4000]
                if cancel_turn(session_id):        # talking over J.A.R.V.I.S.: drop his current answer
                    log.info("Previous answer interrupted by a new request")
                task = asyncio.create_task(brain.handle_user_message(session_id, text, client))
                running_turns[session_id] = task
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            elif kind == "interrupt":             # "Hey Jarvis" said while he was talking
                if cancel_turn(session_id):
                    log.info("Answer interrupted by %s", client.device_name)
            elif kind == "client_tool_result":
                hub.resolve_client_tool(str(msg.get("request_id", "")), msg.get("result"))
            elif kind == "mic":
                client.mic = "listening" if msg.get("state") == "listening" else "muted"
                await hub.broadcast_devices()
            elif kind == "interim":  # partial transcript: shown live on the other devices
                await hub.broadcast({"type": "interim", "text": str(msg.get("text", ""))[:500],
                                     "device": client.device_name}, session_id)
            elif kind == "location":  # real GPS / Wi-Fi position of this device
                coords = valid_coords(msg.get("lat"), msg.get("lon"))
                if coords:
                    client.location = await reverse_geocode(*coords, lang.code)
                    # warm the weather cache now, so the first question about it is answered instantly
                    asyncio.create_task(fetch_forecast(client.location, 3, lang.code))
                    await hub.send(client.client_id, {"type": "location", "place": client.location})
                    await hub.broadcast_devices()
            elif kind == "clear_session":
                await memory.clear_session(session_id)
                await hub.broadcast({"type": "cleared"}, session_id)
            elif kind == "ping":
                await hub.send(client.client_id, {"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WebSocket error")
    finally:
        await hub.unregister(client.client_id)


# ---------------- Static frontend ----------------
@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
