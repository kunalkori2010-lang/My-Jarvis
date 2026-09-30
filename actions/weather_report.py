"""
Weather — real data, not a Google search in a new browser tab.

The tool used to build a search URL and call webbrowser.open(), which is not a
weather report: it opens a page, speaks nothing, and reports nothing back, so the
model had to invent the forecast in its next sentence. That is exactly the kind
of gap where a confident wrong answer is worse than no answer.

Open-Meteo is used because it needs no API key, no account, and no telemetry —
which matters for something that runs unattended on someone's machine.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (compatible; Mark-LIV/1.0)",
       "Accept": "application/json"}

# WMO weather interpretation codes → plain English. Shipping the table beats
# shipping the number: "light rain" is answerable, "51" is not.
_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "moderate rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe hailstorm",
}


def _describe(code) -> str:
    try:
        return _CODES.get(int(code), f"unusual conditions (code {code})")
    except (TypeError, ValueError):
        return "unavailable"


def _get(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


_GEOCODE_CACHE: dict[str, dict] = {}


def _load_geocode_cache() -> None:
    """Restore resolved cities from disk.

    An in-memory cache alone does not help this app at all: it runs one long
    process, and the one call that matters — the morning briefing — happens
    minutes after launch, exactly once. Persisting is what turns the briefing's
    two requests into one, every morning after the first.
    """
    if _GEOCODE_CACHE:
        return
    try:
        from memory.config_manager import load_api_keys
        saved = load_api_keys().get("weather_geocode_cache")
        if isinstance(saved, dict):
            _GEOCODE_CACHE.update({k: v for k, v in saved.items()
                                   if isinstance(v, dict)})
    except Exception:
        pass


def _save_geocode_cache() -> None:
    try:
        from memory.config_manager import _patch_config
        _patch_config(weather_geocode_cache=dict(_GEOCODE_CACHE))
    except Exception:
        # A cache that cannot be written is a slower morning, not a failure.
        pass


def _geocode(city: str) -> dict | None:
    _load_geocode_cache()
    key = city.strip().lower()
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]

    url = ("https://geocoding-api.open-meteo.com/v1/search?name="
           + urllib.parse.quote(city) + "&count=1&language=en&format=json")
    try:
        results = _get(url).get("results") or []
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not results:
        return None
    # Only cache a hit, never a miss — a miss is usually a typo the user will
    # correct, and caching that would make the correction not work.
    _GEOCODE_CACHE[key] = results[0]
    _save_geocode_cache()
    return results[0]


def _fmt_hourly(data: dict) -> str:
    h = data.get("hourly") or {}
    times, codes, temps = h.get("time") or [], h.get("weather_code") or [], \
        h.get("temperature_2m") or []
    if not times:
        return ""
    now = data.get("current", {}).get("time", "")[:13]
    lines = []
    for i, t in enumerate(times):
        if now and t[:13] < now:
            continue
        if len(lines) >= 6:
            break
        try:
            temp = round(float(temps[i]))
        except (IndexError, TypeError, ValueError):
            temp = "?"
        lines.append(f"    {t[11:16]}  {temp}°C  {_describe(codes[i])}")
    return "\n".join(lines)


def _fmt_daily(data: dict) -> str:
    d = data.get("daily") or {}
    times, codes = d.get("time") or [], d.get("weather_code") or []
    highs, lows = d.get("temperature_2m_max") or [], d.get("temperature_2m_min") or []
    lines = []
    for i, t in enumerate(times[:4]):
        try:
            hi, lo = round(float(highs[i])), round(float(lows[i]))
        except (IndexError, TypeError, ValueError):
            hi = lo = "?"
        lines.append(f"    {t}  {lo}°…{hi}°C  {_describe(codes[i])}")
    return "\n".join(lines)


def weather_action(parameters: dict, player=None, session_memory=None) -> str:
    city = parameters.get("city")
    if not city or not isinstance(city, str) or not city.strip():
        msg = "Which city should I get the weather for?"
        _log(msg, player)
        return msg

    city = city.strip()
    try:
        place = _geocode(city)
        if not place:
            msg = (f"I couldn't find a place called '{city}'. Try adding the "
                   f"country — 'Bengaluru, India'.")
            _log(msg, player)
            return msg

        lat, lon = place["latitude"], place["longitude"]
        url = ("https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}"
               "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
               "wind_speed_10m,weather_code,is_day"
               "&hourly=temperature_2m,weather_code"
               "&daily=weather_code,temperature_2m_max,temperature_2m_min"
               "&forecast_days=4&timezone=auto")
        data = _get(url)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        msg = f"I couldn't reach the weather service: {e}"
        _log(msg, player)
        return msg

    cur = data.get("current") or {}
    temp     = round(float(cur.get("temperature_2m", 0)))
    feels    = round(float(cur.get("apparent_temperature", temp)))
    humidity = cur.get("relative_humidity_2m", "?")
    wind     = round(float(cur.get("wind_speed_10m", 0)))
    label    = place.get("name", city)
    region   = ", ".join(x for x in (place.get("admin1"), place.get("country"))
                          if x)

    out = [f"{label}{' (' + region + ')' if region else ''} — right now: "
           f"{temp}°C, feels like {feels}°C, "
           f"{_describe(cur.get('weather_code'))}, "
           f"humidity {humidity}%, wind {wind} km/h."]

    hourly = _fmt_hourly(data)
    if hourly:
        out.append("  Next few hours:\n" + hourly)
    daily = _fmt_daily(data)
    if daily:
        out.append("  Coming up:\n" + daily)

    msg = "\n".join(out)
    _log(msg, player)

    if session_memory:
        try:
            session_memory.set_last_search(query=f"weather in {city}",
                                           response=msg)
        except Exception:
            pass
    return msg


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


TOOL = {
    "name": "weather_report",
    "description": (
        "Current weather and a short forecast for a city, from a live service. "
        "Always use this for questions about weather, temperature, rain or "
        "forecast — never answer those from memory, and never open a browser to "
        "look them up."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "city": {
                "type": "STRING",
                "description": ("City name, ideally with country when "
                                "ambiguous. e.g. 'Bengaluru, India'."),
            },
        },
        "required": ["city"],
    },
    "handler": weather_action,
}
