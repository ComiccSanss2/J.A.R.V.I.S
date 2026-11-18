"""Skill: control of the current device (battery/CPU/RAM status, opening URLs or apps).

The command runs on the device the user spoke from:
- desktop client (Electron): delegated to the client over WebSocket (real data from that PC/Mac);
- browser on the same machine as the server: executed here with psutil;
- remote browser: delegated to the browser (limited: battery, opening URLs).
"""
from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import webbrowser

import psutil

from . import Skill, SkillContext
from .ytm_app import open_in_app
from .remote_agent import via_agent

APP_NAME_RE = re.compile(r"^[\w .\-]{1,64}$", re.UNICODE)


def local_status() -> dict:
    vm = psutil.virtual_memory()
    disk_root = "C:\\" if os.name == "nt" else "/"
    du = psutil.disk_usage(disk_root)
    info = {
        "device": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu_pct": psutil.cpu_percent(interval=0.4),
        "cpu_cores": psutil.cpu_count(logical=True),
        "ram_pct": vm.percent,
        "ram_used_gb": round(vm.used / 1024**3, 1),
        "ram_total_gb": round(vm.total / 1024**3, 1),
        "disk_pct": du.percent,
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
    }
    batt = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if batt is not None:
        info["battery_pct"] = round(batt.percent)
        info["charging"] = bool(batt.power_plugged)
    else:
        info["battery"] = "none (desktop)"
    return info


def local_open_url(url: str) -> dict:
    webbrowser.open(url)
    return {"ok": True, "opened": url}


def local_open_app(app: str) -> dict:
    if sys.platform == "win32":
        try:
            os.startfile(app)  # type: ignore[attr-defined]  # ShellExecute resolves App Paths (e.g. 'notepad', 'chrome')
        except OSError:
            exe = shutil.which(app)
            if not exe:
                return {"ok": False, "error": f"Application not found: {app}"}
            subprocess.Popen([exe], close_fds=True)
    elif sys.platform == "darwin":
        r = subprocess.run(["open", "-a", app], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr.strip() or f"Application not found: {app}"}
    else:
        exe = shutil.which(app)
        if not exe:
            return {"ok": False, "error": f"Application not found: {app}"}
        subprocess.Popen([exe], close_fds=True, start_new_session=True)
    return {"ok": True, "launched": app}


def validate(params: dict) -> tuple[str, dict]:
    action = params.get("action")
    if action not in {"status", "open_url", "open_app"}:
        raise ValueError("action must be 'status', 'open_url' or 'open_app'")
    if action == "open_url":
        url = str(params.get("url", "")).strip()
        if not url:
            raise ValueError("Missing 'url' parameter")
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        return action, {"url": url}
    if action == "open_app":
        app = str(params.get("app", "")).strip()
        if not APP_NAME_RE.match(app):
            raise ValueError("Invalid application name")
        return action, {"app": app}
    return action, {}


async def system_control(params: dict, ctx: SkillContext) -> dict:
    action, args = validate(params)
    origin = ctx.origin
    # 0) YouTube Music links open in the installed desktop app, if there is one (Windows, this computer)
    if action == "open_url" and "music.youtube.com" in args["url"] and (origin is None or origin.is_local):
        show = params.get("show")
        opened = await open_in_app(args["url"], foreground=True if show is None else bool(show))
        if opened:
            return {**opened, "ran_on": origin.device_name if origin else socket.gethostname()}
    # 1) Another computer with the helper agent running (e.g. the MacBook) -> run there natively
    if origin and not origin.is_local and not origin.is_desktop:
        agent_result = await via_agent(ctx, action, args)
        if agent_result is not None:
            return agent_result
    # 2) Desktop client or remote browser -> run on the user's device
    if origin and (origin.is_desktop or not origin.is_local):
        result = await ctx.hub.request_client_tool(origin.client_id, "system_control", {"action": action, **args})
        if isinstance(result, dict):
            result.setdefault("ran_on", origin.device_name)
        return result
    # 3) Browser on the same machine as the server -> run here
    if action == "status":
        result = await asyncio.to_thread(local_status)
    elif action == "open_url":
        result = await asyncio.to_thread(local_open_url, args["url"])
    else:
        result = await asyncio.to_thread(local_open_app, args["app"])
    result["ran_on"] = origin.device_name if origin else socket.gethostname()
    return result


async def list_devices(params: dict, ctx: SkillContext) -> dict:
    devices = await ctx.hub.devices_snapshot()
    return {"devices": [
        {"name": d["name"], "platform": d["platform"], "online": d["online"], "microphone": d["mic"]}
        for d in devices
    ]}


SKILLS = [
    Skill(
        name="system_control",
        label="SYSTEM_CTRL",
        description=(
            "Commands on the device the user is speaking from. "
            "action='status': battery, CPU, RAM, disk, uptime. "
            "action='open_url' (requires url): opens a website in the browser. "
            "action='open_app' (requires app, e.g. 'Spotify', 'notepad', 'Calculator'): launches an application."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "open_url", "open_app"]},
                "url": {"type": "string", "description": "URL to open (open_url only)."},
                "app": {"type": "string", "description": "Name of the app to launch (open_app only)."},
                "show": {"type": "boolean", "description": "YouTube Music only: false = open it minimized in the background."},
            },
            "required": ["action"],
        },
        handler=system_control,
    ),
    Skill(
        name="list_devices",
        label="DEVICE_MESH",
        description="Lists the known J.A.R.V.I.S. devices (PC, Mac...) with their online and microphone status.",
        input_schema={"type": "object", "properties": {}},
        handler=list_devices,
    ),
]
