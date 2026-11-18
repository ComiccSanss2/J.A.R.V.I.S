"""Modular Skills system.

Each module in this package exposes a `SKILLS` list of `Skill` objects
(run by the backend or delegated to the device) and, optionally,
a `server_tools(settings)` function returning Anthropic-side tools (e.g. web search) passed as-is.

To add a skill: create `backend/skills/my_skill.py` with
`SKILLS = [Skill(name=..., description=..., input_schema=..., handler=...)]`.
It is loaded automatically at startup.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from ..config import Settings
    from ..hub import Client, Hub
    from ..memory import Memory

log = logging.getLogger("jarvis.skills")


@dataclass
class SkillContext:
    session_id: str
    origin: "Client | None"
    hub: "Hub"
    memory: "Memory"
    settings: "Settings"


Handler = Callable[[dict[str, Any], SkillContext], Awaitable[Any]]


@dataclass
class Skill:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    label: str = ""  # label shown in the HUD log, e.g. WEATHER_API
    enabled: Callable[["Settings"], bool] = field(default=lambda s: True)

    def tool_definition(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    @property
    def hud_label(self) -> str:
        return self.label or self.name.upper()


class SkillRegistry:
    def __init__(self, settings: "Settings"):
        self.settings = settings
        self.skills: dict[str, Skill] = {}
        self.server_tools: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        for mod_info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
            try:
                module = importlib.import_module(f"{__name__}.{mod_info.name}")
            except Exception:
                log.exception("Could not load skill %s", mod_info.name)
                continue
            for skill in getattr(module, "SKILLS", []):
                if skill.enabled(self.settings):
                    self.skills[skill.name] = skill
            factory = getattr(module, "server_tools", None)
            if callable(factory):
                self.server_tools.extend(factory(self.settings))
        log.info("Skills loaded: %s | server tools: %s",
                 ", ".join(self.skills), ", ".join(t["name"] for t in self.server_tools) or "-")

    def tool_definitions(self, include_server_tools: bool = True) -> list[dict[str, Any]]:
        defs = [s.tool_definition() for s in self.skills.values()]
        if include_server_tools:
            defs.extend(self.server_tools)
        return defs

    def catalog(self) -> list[dict[str, str]]:
        items = [{"name": s.name, "label": s.hud_label, "description": s.description.split(". ")[0].rstrip(".")}
                 for s in self.skills.values()]
        items += [{"name": t["name"], "label": t["name"].upper(), "description": "Anthropic server tool"}
                  for t in self.server_tools]
        return items

    def validate(self, name: str, tool_input: Any) -> str | None:
        """Checks a tool input against its schema (needed because inputs are streamed unvalidated).

        Returns an error message, or None when the input is valid.
        """
        skill = self.skills.get(name)
        if not skill:
            return f"Unknown skill: {name}"
        if not isinstance(tool_input, dict):
            return "Tool input must be a JSON object"
        schema = skill.input_schema
        for key in schema.get("required", []):
            if key not in tool_input:
                return f"Missing required field '{key}'"
        types = {"string": str, "integer": int, "number": (int, float), "object": dict, "boolean": bool, "array": list}
        for key, value in tool_input.items():
            spec = schema.get("properties", {}).get(key)
            if spec is None:
                continue
            expected = types.get(spec.get("type"))
            if expected and (not isinstance(value, expected) or (spec.get("type") == "integer" and isinstance(value, bool))):
                return f"Field '{key}' must be of type {spec['type']}"
            if "enum" in spec and value not in spec["enum"]:
                return f"Field '{key}' must be one of {spec['enum']}"
        return None

    async def execute(self, name: str, tool_input: dict[str, Any], ctx: SkillContext) -> Any:
        skill = self.skills.get(name)
        if not skill:
            raise ValueError(f"Unknown skill: {name}")
        return await skill.handler(tool_input or {}, ctx)
