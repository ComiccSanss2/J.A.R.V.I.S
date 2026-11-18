"""Skill: current weather and forecast via Open-Meteo (free, no key)."""
from __future__ import annotations

import time

import httpx

from ..i18n import lang
from . import Skill, SkillContext

WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain", 71: "light snow", 73: "snow",
    75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    99: "severe thunderstorm with hail",
}

WMO_IT = {
    0: "sereno", 1: "prevalentemente sereno", 2: "parzialmente nuvoloso", 3: "coperto",
    45: "nebbia", 48: "nebbia con brina", 51: "pioviggine leggera", 53: "pioviggine", 55: "pioviggine intensa",
    56: "pioviggine gelata", 57: "pioviggine gelata intensa", 61: "pioggia leggera", 63: "pioggia",
    65: "pioggia forte", 66: "pioggia gelata", 67: "pioggia gelata forte", 71: "neve leggera", 73: "neve",
    75: "neve forte", 77: "granuli di neve", 80: "rovesci leggeri", 81: "rovesci", 82: "rovesci violenti",
    85: "rovesci di neve", 86: "forti rovesci di neve", 95: "temporale", 96: "temporale con grandine",
    99: "temporale forte con grandine",
}
WMO_FR = {
    0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
    45: "brouillard", 48: "brouillard givrant", 51: "bruine légère", 53: "bruine", 55: "bruine dense",
    56: "bruine verglaçante", 57: "forte bruine verglaçante", 61: "pluie faible", 63: "pluie",
    65: "forte pluie", 66: "pluie verglaçante", 67: "forte pluie verglaçante", 71: "neige faible", 73: "neige",
    75: "forte neige", 77: "grains de neige", 80: "averses faibles", 81: "averses", 82: "violentes averses",
    85: "averses de neige", 86: "fortes averses de neige", 95: "orage", 96: "orage avec grêle",
    99: "violent orage avec grêle",
}
CONDITIONS = {"en": WMO, "it": WMO_IT, "fr": WMO_FR}

_geo_cache: dict[str, dict] = {}
_wx_cache: dict[tuple, tuple[float, dict]] = {}
CACHE_TTL = 600


async def geocode(city: str, lang: str = "en") -> dict:
    key = f"{city.strip().lower()}|{lang}"
    if key in _geo_cache:
        return _geo_cache[key]
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": lang, "format": "json"},
        )
        r.raise_for_status()
        results = r.json().get("results") or []
    if not results:
        raise ValueError(f"Location not found: {city}")
    g = results[0]
    place = {
        "name": g["name"], "country": g.get("country", ""), "admin": g.get("admin1", ""),
        "latitude": g["latitude"], "longitude": g["longitude"],
    }
    _geo_cache[key] = place
    return place


async def fetch_forecast(place: dict, days: int = 3, lang: str = "en") -> dict:
    """Weather for a resolved place {name, admin, country, latitude, longitude}."""
    days = max(1, min(int(days or 3), 7))
    cache_key = (round(place["latitude"], 2), round(place["longitude"], 2), days, lang)
    names = CONDITIONS.get(lang, WMO)
    hit = _wx_cache.get(cache_key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"], "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto", "forecast_days": days,
            },
        )
        r.raise_for_status()
        data = r.json()
    cur = data["current"]
    daily = data["daily"]
    result = {
        "location": ", ".join(p for p in (place["name"], place.get("admin", ""), place.get("country", "")) if p),
        "timezone": data.get("timezone"),
        "current": {
            "temperature_c": cur["temperature_2m"],
            "feels_like_c": cur["apparent_temperature"],
            "humidity_pct": cur["relative_humidity_2m"],
            "wind_kmh": cur["wind_speed_10m"],
            "conditions": names.get(cur["weather_code"], "n/a"),
            "code": cur["weather_code"],
            "daytime": bool(cur["is_day"]),
        },
        "forecast": [
            {
                "date": daily["time"][i],
                "conditions": names.get(daily["weather_code"][i], "n/a"),
                "min_c": daily["temperature_2m_min"][i],
                "max_c": daily["temperature_2m_max"][i],
                "rain_chance_pct": daily["precipitation_probability_max"][i],
            }
            for i in range(len(daily["time"]))
        ],
    }
    _wx_cache[cache_key] = (time.time(), result)
    return result


async def fetch_weather(city: str, days: int = 3, lang: str = "en") -> dict:
    """Weather for a city name."""
    return await fetch_forecast(await geocode(city, lang), days, lang)


async def get_weather(params: dict, ctx: SkillContext) -> dict:
    days = params.get("days", 3)
    city = (params.get("city") or "").strip()
    if city:
        return await fetch_weather(city, days, lang.code)
    # No city named: use the real position of the device the user is speaking from
    if ctx.origin and ctx.origin.location:
        result = await fetch_forecast(ctx.origin.location, days, lang.code)
        return {**result, "source": f"current position of {ctx.origin.device_name}"}
    result = await fetch_weather(ctx.settings.default_city, days, lang.code)
    return {**result, "source": "default city (device position unavailable)"}


SKILLS = [
    Skill(
        name="get_weather",
        label="WEATHER_API",
        description=(
            "Current weather (temperature, conditions, wind, humidity) and daily forecast up to 7 days "
            "for a city. If the user names no city, it uses the real current position of the device they are speaking from."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name, e.g. 'London'. Optional."},
                "days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "Forecast days (default 3)."},
            },
        },
        handler=get_weather,
    )
]
