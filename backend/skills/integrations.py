"""Integration skills: web search (Anthropic server tool), HTTP webhooks and local scripts.

Webhooks and scripts are configured in `config/integrations.json` (see README):
the model can only invoke the entries listed there, never arbitrary URLs or commands.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from typing import Any

import httpx

from ..config import ROOT_DIR, settings
from . import Skill, SkillContext

log = logging.getLogger("jarvis.integrations")
CONFIG_FILE = settings.config_dir / "integrations.json"
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"webhooks": [], "scripts": []}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("integrations.json is not valid JSON: %s", exc)
        return {"webhooks": [], "scripts": []}
    data["webhooks"] = [w for w in data.get("webhooks", []) if w.get("enabled", True)]
    data["scripts"] = [s for s in data.get("scripts", []) if s.get("enabled", True)]
    return data


CONFIG = load_config()


# ---------------- Web search (runs on Anthropic's servers) ----------------
# Models that support the newer web search with dynamic filtering; others (e.g. Haiku) use the basic one
DYNAMIC_SEARCH_PREFIXES = ("claude-opus-", "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-", "claude-mythos-")


def server_tools(s) -> list[dict]:
    if not s.enable_web_search:
        return []
    kind = "web_search_20260209" if s.model.startswith(DYNAMIC_SEARCH_PREFIXES) else "web_search_20250305"
    return [{"type": kind, "name": "web_search", "max_uses": 3}]


# ---------------- HTTP webhooks ----------------
async def custom_api_webhook(params: dict, ctx: SkillContext) -> dict:
    name = params.get("webhook")
    hook = next((w for w in CONFIG["webhooks"] if w["name"] == name), None)
    if not hook:
        return {"error": f"Webhook '{name}' is not configured. Available: {[w['name'] for w in CONFIG['webhooks']]}"}
    method = hook.get("method", "POST").upper()
    payload = {**hook.get("default_payload", {}), **(params.get("payload") or {})}
    headers = _expand_env(hook.get("headers", {}))
    url = _expand_env(hook["url"])
    async with httpx.AsyncClient(timeout=hook.get("timeout", 15)) as client:
        if method == "GET":
            r = await client.get(url, params=payload, headers=headers)
        else:
            r = await client.request(method, url, json=payload, headers=headers)
    body = r.text[:2000]
    try:
        body = r.json()
    except ValueError:
        pass
    return {"status": r.status_code, "ok": r.is_success, "response": body}


# ---------------- Local scripts (on the server) ----------------
async def run_local_script(params: dict, ctx: SkillContext) -> dict:
    name = params.get("script")
    spec = next((s for s in CONFIG["scripts"] if s["name"] == name), None)
    if not spec:
        return {"error": f"Script '{name}' is not configured. Available: {[s['name'] for s in CONFIG['scripts']]}"}
    cmd = [sys.executable if part == "{python}" else part for part in spec["command"]]
    arg = params.get("argument")
    if arg is not None and spec.get("accepts_argument", False):
        cmd.append(str(arg)[:500])  # passed as a single argv entry, never through a shell
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(ROOT_DIR), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=spec.get("timeout", 30))
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "Script timed out"}
    return {
        "exit_code": proc.returncode,
        "stdout": out.decode(errors="replace")[-2000:],
        "stderr": err.decode(errors="replace")[-1000:],
    }


def _describe(items: list[dict]) -> str:
    return "; ".join(f"'{i['name']}': {i.get('description', '')}" for i in items) or "none"


SKILLS = [
    Skill(
        name="custom_api_webhook",
        label="WEBHOOK",
        description=(
            "Calls a preconfigured external API/webhook (home automation, calendar, email, automations). "
            f"Available webhooks: {_describe(CONFIG['webhooks'])}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "webhook": {"type": "string", "enum": [w["name"] for w in CONFIG["webhooks"]] or ["none"]},
                "payload": {"type": "object", "description": "JSON data to send (optional)."},
            },
            "required": ["webhook"],
        },
        handler=custom_api_webhook,
        enabled=lambda s: bool(CONFIG["webhooks"]),
    ),
    Skill(
        name="run_local_script",
        label="LOCAL_SCRIPT",
        description=f"Runs a preconfigured local script on the server. Available scripts: {_describe(CONFIG['scripts'])}.",
        input_schema={
            "type": "object",
            "properties": {
                "script": {"type": "string", "enum": [s["name"] for s in CONFIG["scripts"]] or ["none"]},
                "argument": {"type": "string", "description": "Optional text argument (if the script accepts one)."},
            },
            "required": ["script"],
        },
        handler=run_local_script,
        enabled=lambda s: bool(CONFIG["scripts"]),
    ),
]
