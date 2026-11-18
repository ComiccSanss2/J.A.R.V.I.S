"""Developer skills: create/read files and run terminal commands on the computer hosting the server.

Safety model
- Files: read and written only inside the workspace folder (JARVIS_WORKSPACE); paths that escape it are refused.
- Terminal: every command needs the user's approval on the HUD (click or say "approve") unless
  JARVIS_CONFIRM_COMMANDS=false. Commands start in the workspace, with a timeout.
- Only available to a device on the server machine itself, or to every device when a shared
  JARVIS_ACCESS_TOKEN protects the server (otherwise anyone on the LAN could ask for commands).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from ..config import settings
from . import Skill, SkillContext

MAX_READ_CHARS = 20_000
MAX_OUTPUT_CHARS = 6_000
MAX_LIST_ENTRIES = 200
APPROVAL_TIMEOUT = 120


def workspace() -> Path:
    ws = settings.workspace
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def safe_path(raw: str | None) -> Path:
    """Resolve a user/model-supplied path inside the workspace; refuse anything that escapes it."""
    ws = workspace().resolve()
    target = (ws / (raw or ".")).resolve()
    if not target.is_relative_to(ws):
        raise PermissionError(f"Path outside the workspace refused: {raw}. Workspace: {ws}")
    return target


def check_origin(ctx: SkillContext) -> str | None:
    origin = ctx.origin
    if origin is None:
        return "No device to ask for approval."
    if not (origin.is_local or settings.access_token):
        return ("Developer tools are only available from the computer running the server "
                "(or set JARVIS_ACCESS_TOKEN to allow other devices).")
    return None


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"...[{len(text) - limit} characters cut]...\n" + text[-limit:]


# ---------------- files ----------------
async def list_files(params: dict, ctx: SkillContext) -> dict:
    if problem := check_origin(ctx):
        return {"error": problem}
    target = safe_path(params.get("path"))
    if not target.exists():
        return {"error": f"Not found: {params.get('path')}"}
    if target.is_file():
        return {"file": str(target.relative_to(workspace().resolve())), "size_bytes": target.stat().st_size}
    entries = []
    for p in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:MAX_LIST_ENTRIES]:
        entries.append(p.name + ("/" if p.is_dir() else f"  ({p.stat().st_size} B)"))
    return {"workspace": str(workspace().resolve()), "path": params.get("path") or ".", "entries": entries}


async def read_file(params: dict, ctx: SkillContext) -> dict:
    if problem := check_origin(ctx):
        return {"error": problem}
    target = safe_path(params["path"])
    if not target.is_file():
        return {"error": f"File not found: {params['path']}"}
    text = target.read_text(encoding="utf-8", errors="replace")
    return {"path": params["path"], "content": text[:MAX_READ_CHARS],
            "truncated": len(text) > MAX_READ_CHARS}


async def write_file(params: dict, ctx: SkillContext) -> dict:
    if problem := check_origin(ctx):
        return {"error": problem}
    target = safe_path(params["path"])
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(params["content"], encoding="utf-8")
    return {"ok": True, "path": str(target), "action": "overwritten" if existed else "created",
            "characters": len(params["content"])}


# ---------------- terminal ----------------
def _shell_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        # UTF-8 output so accents and symbols come back readable
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
    shell = os.environ.get("SHELL", "/bin/bash")
    return [shell, "-lc", command]


def _run(command: str, cwd: Path, timeout: int) -> dict:
    try:
        proc = subprocess.run(_shell_argv(command), cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        return {"exit_code": proc.returncode,
                "stdout": _tail(proc.stdout, MAX_OUTPUT_CHARS), "stderr": _tail(proc.stderr, MAX_OUTPUT_CHARS // 2)}
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return {"error": f"Command stopped after {timeout}s timeout", "stdout": _tail(out, MAX_OUTPUT_CHARS)}
    except FileNotFoundError as exc:
        return {"error": f"Shell not found: {exc}"}


async def run_command(params: dict, ctx: SkillContext) -> dict:
    if problem := check_origin(ctx):
        return {"error": problem}
    command = params["command"].strip()
    if not command:
        return {"error": "Empty command"}
    cwd = safe_path(params.get("cwd"))
    if not cwd.is_dir():
        return {"error": f"Folder not found: {params.get('cwd')}"}
    timeout = max(5, min(int(params.get("timeout_seconds") or 120), 600))

    if settings.confirm_commands:
        decision = await ctx.hub.request_client_tool(
            ctx.origin.client_id, "approval",
            {"kind": "command", "command": command, "cwd": str(cwd), "reason": params.get("reason", "")},
            timeout=APPROVAL_TIMEOUT,
        )
        if decision is not True:
            return {"error": "The user did not approve this command, so it was NOT run.", "approved": False}

    result = await asyncio.to_thread(_run, command, cwd, timeout)
    return {"command": command, "cwd": str(cwd), **result}


def _enabled(s) -> bool:
    return s.dev_tools


PATH_HELP = "Path relative to the workspace folder, e.g. 'my_app/main.py'."

SKILLS = [
    Skill(
        name="list_files",
        label="FS_LIST",
        description="Lists files and folders in the user's coding workspace (or a sub-folder of it).",
        input_schema={"type": "object", "properties": {"path": {"type": "string", "description": PATH_HELP + " Default: the workspace root."}}},
        handler=list_files, enabled=_enabled,
    ),
    Skill(
        name="read_file",
        label="FS_READ",
        description="Reads a text file from the user's coding workspace.",
        input_schema={"type": "object", "properties": {"path": {"type": "string", "description": PATH_HELP}}, "required": ["path"]},
        handler=read_file, enabled=_enabled,
    ),
    Skill(
        name="write_file",
        label="FS_WRITE",
        description=(
            "Creates or overwrites a text file (code, scripts, notes, HTML...) in the user's coding workspace, "
            "creating folders as needed. Always write the COMPLETE file content."
        ),
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "description": PATH_HELP},
            "content": {"type": "string", "description": "Full file content."}},
            "required": ["path", "content"]},
        handler=write_file, enabled=_enabled,
    ),
    Skill(
        name="run_command",
        label="TERMINAL",
        description=(
            "Runs a terminal command on the user's computer (PowerShell on Windows, the login shell on macOS) and "
            "returns exit code and output. The user must approve every command on screen first. Use it to run code, "
            "install packages, use git, etc. Commands start in the workspace folder. Do not start programs that never "
            "exit (servers, watchers) unless the user asks; they are stopped at the timeout."
        ),
        input_schema={"type": "object", "properties": {
            "command": {"type": "string", "description": "The exact command line to run."},
            "cwd": {"type": "string", "description": "Folder to run in, relative to the workspace. Default: workspace root."},
            "reason": {"type": "string", "description": "One short sentence shown to the user explaining why."},
            "timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 600, "description": "Default 120."}},
            "required": ["command"]},
        handler=run_command, enabled=_enabled,
    ),
]
