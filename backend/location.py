"""Real device location: the browser sends GPS/Wi-Fi coordinates, the server turns them into a place name.

Coordinates are only sent to this server, to OpenStreetMap Nominatim (place name) and Open-Meteo (weather).
"""
from __future__ import annotations

import logging
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

log = logging.getLogger("jarvis.location")

# Nominatim's usage policy asks for an identifying User-Agent (generic, no personal data)
USER_AGENT = "JARVIS-OS/1.0 (self-hosted personal assistant)"
_reverse_cache: dict[tuple[float, float, str], dict] = {}


def valid_coords(lat, lon) -> tuple[float, float] | None:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def valid_timezone(tz: str | None) -> str | None:
    if not tz or len(tz) > 64:
        return None
    try:
        ZoneInfo(tz)
        return tz
    except (ZoneInfoNotFoundError, ValueError):
        return None


async def reverse_geocode(lat: float, lon: float, lang: str = "en") -> dict:
    """Coordinates -> {name, admin, country, latitude, longitude, label}. Never raises."""
    key = (round(lat, 2), round(lon, 2), lang)  # ~1 km: avoids a lookup for every small GPS move
    if key in _reverse_cache:
        return _reverse_cache[key]
    place = {"name": f"{lat:.3f}, {lon:.3f}", "admin": "", "country": "", "latitude": lat, "longitude": lon}
    try:
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent": USER_AGENT}) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10, "accept-language": lang},
            )
            r.raise_for_status()
            addr = r.json().get("address", {})
        name = (addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
                or addr.get("county") or place["name"])
        place.update(name=name, admin=addr.get("state", ""), country=addr.get("country", ""))
    except Exception as exc:
        log.warning("Reverse geocoding failed: %s", exc)
    place["label"] = ", ".join(p for p in (place["name"], place["admin"], place["country"]) if p)
    _reverse_cache[key] = place
    return place
