"""Skill: current date and time."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import Skill, SkillContext

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def now_info(tz_name: str) -> dict:
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
        tz_name = "UTC"
    now = datetime.now(tz)
    return {
        "iso": now.isoformat(timespec="seconds"),
        "weekday": DAYS[now.weekday()],
        "date": f"{now.day} {MONTHS[now.month - 1]} {now.year}",
        "time": now.strftime("%H:%M:%S"),
        "timezone": tz_name,
        "iso_week": now.isocalendar().week,
    }


async def get_current_datetime(params: dict, ctx: SkillContext) -> dict:
    # Default: the time zone of the device the user is speaking from
    device_tz = ctx.origin.timezone if ctx.origin else None
    return now_info(params.get("timezone") or device_tz or ctx.settings.timezone)


SKILLS = [
    Skill(
        name="get_current_datetime",
        label="DATETIME",
        description=(
            "Returns the exact date, day of the week and current time. "
            "Use it whenever date/time or anything relative to 'today', 'tomorrow', etc. is needed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "Optional IANA time zone, e.g. 'Europe/London' or 'America/New_York'. Default: the time zone of the user's current device.",
                }
            },
        },
        handler=get_current_datetime,
    )
]
