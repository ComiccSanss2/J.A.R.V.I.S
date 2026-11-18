"""Skill: long-term memory (facts/preferences shared across every device)."""
from __future__ import annotations

import re

from . import Skill, SkillContext

KEY_RE = re.compile(r"[^a-z0-9_]+")


def _norm(key: str) -> str:
    return KEY_RE.sub("_", key.strip().lower()).strip("_")[:64]


async def remember_fact(params: dict, ctx: SkillContext) -> dict:
    key, value = _norm(params.get("key", "")), str(params.get("value", "")).strip()[:500]
    if not key or not value:
        return {"error": "key and value are required"}
    await ctx.memory.set_fact(key, value)
    await ctx.hub.broadcast({"type": "facts", "facts": await ctx.memory.facts()})
    return {"ok": True, "stored": {key: value}}


async def forget_fact(params: dict, ctx: SkillContext) -> dict:
    key = _norm(params.get("key", ""))
    removed = await ctx.memory.delete_fact(key)
    await ctx.hub.broadcast({"type": "facts", "facts": await ctx.memory.facts()})
    return {"ok": removed, "key": key}


SKILLS = [
    Skill(
        name="remember_fact",
        label="MEMORY_WRITE",
        description=(
            "Saves a fact or preference about the user to permanent memory (e.g. name, home city, habits), "
            "shared across every device. Use it when the user asks you to remember something."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short snake_case key, e.g. 'home_city'."},
                "value": {"type": "string", "description": "Value to remember."},
            },
            "required": ["key", "value"],
        },
        handler=remember_fact,
    ),
    Skill(
        name="forget_fact",
        label="MEMORY_ERASE",
        description="Deletes a fact from permanent memory.",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
        handler=forget_fact,
    ),
]
