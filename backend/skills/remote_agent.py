"""Helper: run an action on another computer through its helper agent (frontend/agent/mac_agent.js).

No SKILLS here. When the user speaks from a device other than the server PC (e.g. the MacBook) and that
device runs the helper, volume / music / apps / screenshots happen on THAT device.
"""
from __future__ import annotations

from . import SkillContext


def remote_origin(ctx: SkillContext) -> bool:
    return bool(ctx.origin and not ctx.origin.is_local)


async def via_agent(ctx: SkillContext, action: str, params: dict) -> dict | None:
    """Result from the agent, or None when that device has no helper running."""
    if not remote_origin(ctx):
        return None
    return await ctx.hub.request_agent(ctx.origin.device_name, action, params)


def no_agent_error(ctx: SkillContext, what: str) -> dict:
    name = ctx.origin.device_name if ctx.origin else "that device"
    return {"error": f"{what} on {name} needs the J.A.R.V.I.S. helper running on {name} "
                     f"(see README, section 'Mac helper'). Without it this only works on the server PC."}
