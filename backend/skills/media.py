"""Skills: play music (YouTube Music / YouTube) and control playback and volume on the user's computer.

"Play Down for me by Lil Tecca" -> finds the song on YouTube Music and opens it, playing, in the
default browser of the device the user is speaking from. No API key needed.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote_plus

import httpx

from . import Skill, SkillContext
from .system_control import system_control
from .ytm_app import find_app, open_in_app
from . import app_audio
from .remote_agent import no_agent_error, remote_origin, via_agent

BROWSERS = {"brave.exe", "chrome.exe", "msedge.exe"}

log = logging.getLogger("jarvis.media")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
YTM_SEARCH = "https://music.youtube.com/youtubei/v1/search?prettyPrint=false"
YTM_CLIENT = {"clientName": "WEB_REMIX", "clientVersion": "1.20250101.01.00", "hl": "en", "gl": "US"}
# YouTube Music search filters (same values the website uses)
YTM_FILTERS = {"song": "EgWKAQIIAWoMEA4QChADEAQQCRAF", "video": "EgWKAQIQAWoMEA4QChADEAQQCRAF",
               "album": "EgWKAQIYAWoMEA4QChADEAQQCRAF", "playlist": "EgeKAQQoAEABagwQDhAKEAMQBBAJEAU="}


def _video_ids(node):
    if isinstance(node, dict):
        vid = node.get("videoId")
        if isinstance(vid, str) and len(vid) == 11:
            yield vid
        for v in node.values():
            yield from _video_ids(v)
    elif isinstance(node, list):
        for v in node:
            yield from _video_ids(v)


async def find_on_youtube_music(query: str, kind: str = "song") -> str | None:
    """First matching videoId on YouTube Music (filtered by kind, then unfiltered)."""
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": UA, "Origin": "https://music.youtube.com",
                                                     "Referer": "https://music.youtube.com/"}) as client:
        for params in (YTM_FILTERS.get(kind), None):
            body = {"context": {"client": YTM_CLIENT}, "query": query}
            if params:
                body["params"] = params
            try:
                r = await client.post(YTM_SEARCH, json=body)
                r.raise_for_status()
                vid = next(_video_ids(r.json()), None)
                if vid:
                    return vid
            except Exception as exc:
                log.warning("YouTube Music search failed (%s): %s", "filtered" if params else "plain", exc)
    return None


async def video_title(vid: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get("https://www.youtube.com/oembed",
                                 params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"})
            if r.status_code == 200:
                d = r.json()
                return {"title": d.get("title"), "channel": (d.get("author_name") or "").replace(" - Topic", "")}
    except Exception:
        pass
    return {}


async def play_music(params: dict, ctx: SkillContext) -> dict:
    query = str(params.get("query", "")).strip()
    if not query:
        return {"error": "Tell me what to play"}
    service = params.get("service") or "youtube_music"
    kind = params.get("kind") or "song"
    vid = await find_on_youtube_music(query, kind)
    if vid:
        base = "https://music.youtube.com" if service == "youtube_music" else "https://www.youtube.com"
        url = f"{base}/watch?v={vid}"
        info = await video_title(vid)
    else:  # nothing found: at least open the search page
        url = (f"https://music.youtube.com/search?q={quote_plus(query)}" if service == "youtube_music"
               else f"https://www.youtube.com/results?search_query={quote_plus(query)}")
        info = {"note": "No exact match found: opened the search results instead."}
    # YouTube Music desktop app (minimized in the background by default, see JARVIS_MUSIC_FOREGROUND)
    if service == "youtube_music" and (ctx.origin is None or ctx.origin.is_local):
        show = params.get("show")
        in_app = await open_in_app(url, foreground=None if show is None else bool(show))
        if in_app:
            return {"ok": True, "playing": info, "url": url, **in_app}
    opened = await system_control({"action": "open_url", "url": url, "show": True}, ctx)
    if isinstance(opened, dict) and opened.get("error"):
        return {"error": opened["error"], "url": url}
    return {"ok": True, "playing": info, "url": url, "service": service,
            "ran_on": opened.get("ran_on") if isinstance(opened, dict) else None}


# ---------------- media keys & volume ----------------
VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
      "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}


def _press_windows(action: str, times: int) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    for _ in range(times):
        user32.keybd_event(VK[action], 0, 1, 0)       # KEYEVENTF_EXTENDEDKEY
        user32.keybd_event(VK[action], 0, 1 | 2, 0)   # + KEYEVENTF_KEYUP


def _mac(action: str, times: int) -> None:
    scripts = {
        "volume_up": f"set volume output volume ((output volume of (get volume settings)) + {6 * times})",
        "volume_down": f"set volume output volume ((output volume of (get volume settings)) - {6 * times})",
        "mute": "set volume output muted (not (output muted of (get volume settings)))",
    }
    if action not in scripts:
        raise RuntimeError("On macOS only volume and mute are supported; use the player controls for the rest.")
    subprocess.run(["osascript", "-e", scripts[action]], check=True, timeout=5)


async def media_control(params: dict, ctx: SkillContext) -> dict:
    action = params.get("action")
    if action not in VK:
        return {"error": f"Unknown action. Use one of {list(VK)}"}
    origin = ctx.origin
    if remote_origin(ctx):          # e.g. the MacBook: its helper agent does it there
        if action.startswith("volume") or action == "mute":
            steps = max(1, min(int(params.get("steps") or 1), 10))
            sub = {"volume_up": "up", "volume_down": "down", "mute": "mute"}[action]
            r = await via_agent(ctx, "volume", {"action": sub, "percent": 10 * steps})
        else:
            r = await via_agent(ctx, "media", {"action": action})
        return r if r is not None else no_agent_error(ctx, "Music and volume control")
    # one volume key step is 2% on Windows: 5 presses = 10%
    times = max(1, min(int(params.get("steps") or 1), 10)) * (5 if action.startswith("volume") else 1)
    try:
        if sys.platform == "win32":
            await asyncio.to_thread(_press_windows, action, times)
        elif sys.platform == "darwin":
            await asyncio.to_thread(_mac, action, max(1, times // 5))
        else:
            return {"error": "Media control is not supported on this system"}
    except Exception as exc:
        return {"error": str(exc)}
    return {"ok": True, "action": action, "ran_on": origin.device_name if origin else None}


def _app_processes(app: str) -> set[str]:
    """Which processes make the sound of `app` (as named by the user)."""
    a = app.lower().strip()
    if "music" in a or "ytm" in a or "youtube" in a:
        # The YouTube Music app is a browser "installed app": its sound comes from that browser's process
        yt = find_app()
        if yt:
            folder = Path(yt["target"]).parent
            exes = {f.name.lower() for f in folder.glob("*.exe")} & BROWSERS
            if exes:
                return exes
        return set(BROWSERS)
    token = a.replace(" ", "").removesuffix(".exe")
    playing = {x["process"] for x in app_audio.list_sessions()}
    return {name for name in playing if token and token in name.replace(" ", "")} or {token + ".exe"}


async def app_volume(params: dict, ctx: SkillContext) -> dict:
    origin = ctx.origin
    if remote_origin(ctx):
        r = await via_agent(ctx, "app_volume", {"app": params.get("app") or "youtube music",
                                                "action": params.get("action"), "percent": params.get("percent")})
        return r if r is not None else no_agent_error(ctx, "Volume control")
    if sys.platform != "win32":
        return {"error": "App volume is only available on Windows; use media_control for the system volume."}
    app = str(params.get("app") or "youtube music")
    action = params.get("action")
    amount = int(params.get("percent") or (15 if action in ("up", "down") else 50))
    names = await asyncio.to_thread(_app_processes, app)
    result = await asyncio.to_thread(app_audio.change_volume, names, action, max(1, min(amount, 100)))
    return {**result, "app": app}


SKILLS = [
    Skill(
        name="play_music",
        label="MUSIC",
        description=(
            "Plays a song, artist, album or playlist: finds it on YouTube Music (default) or YouTube and opens it, "
            "already playing, in the browser of the user's device. Use it whenever the user asks to play / put on "
            "music, e.g. 'put Down for me by Lil Tecca'."
        ),
        input_schema={"type": "object", "properties": {
            "query": {"type": "string", "description": "What to search, e.g. 'Down for me Lil Tecca'. Include the artist if known."},
            "kind": {"type": "string", "enum": ["song", "video", "album", "playlist"], "description": "Default: song."},
            "service": {"type": "string", "enum": ["youtube_music", "youtube"], "description": "Default: youtube_music."},
            "show": {"type": "boolean", "description": "true only if the user asks to see / show the player; otherwise leave it out (the user's default applies)."}},
            "required": ["query"]},
        handler=play_music,
    ),
    Skill(
        name="media_control",
        label="MEDIA_KEYS",
        description=(
            "Presses the computer's media keys: play_pause, next, previous, stop, and the WINDOWS MASTER volume "
            "(volume_up, volume_down, mute) for the whole computer. Play/pause/next work with whatever is playing. "
            "For the volume of ONE app (YouTube Music, Spotify, Discord...) use app_volume instead. "
            "steps = how many 10% volume steps (default 1)."
        ),
        input_schema={"type": "object", "properties": {
            "action": {"type": "string", "enum": list(VK)},
            "steps": {"type": "integer", "minimum": 1, "maximum": 10}},
            "required": ["action"]},
        handler=media_control,
    ),
    Skill(
        name="app_volume",
        label="APP_VOLUME",
        description=(
            "Changes the volume of ONE application only (Windows volume mixer), not the computer's master volume. "
            "Use it whenever the user talks about the volume of the music / YouTube Music / Spotify / Discord / a game. "
            "action: up | down (by percent, default 15) | set (to percent) | mute | unmute."
        ),
        input_schema={"type": "object", "properties": {
            "app": {"type": "string", "description": "App name as the user says it, e.g. 'youtube music', 'discord', 'spotify'. Default: youtube music."},
            "action": {"type": "string", "enum": ["up", "down", "set", "mute", "unmute"]},
            "percent": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Step for up/down (default 15), or target level for set."}},
            "required": ["action"]},
        handler=app_volume,
    ),
]
