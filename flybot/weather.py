"""Weather and air quality where the robot is, from Open-Meteo (free, no key)."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

# WMO weather codes -> short Thai
CODES = {0: "ฟ้าใส", 1: "แดดเป็นส่วนใหญ่", 2: "มีเมฆบางส่วน", 3: "เมฆมาก", 45: "หมอก", 48: "หมอก",
         51: "ฝนปรอยๆ", 53: "ฝนปรอยๆ", 55: "ฝนปรอยหนัก", 61: "ฝนเล็กน้อย", 63: "ฝนปานกลาง", 65: "ฝนหนัก",
         80: "ฝนซู่", 81: "ฝนซู่ปานกลาง", 82: "ฝนซู่หนัก", 95: "พายุฝนฟ้าคะนอง", 96: "พายุลูกเห็บ", 99: "พายุลูกเห็บ"}


class Weather:
    def __init__(self, latitude: float = 16.43, longitude: float = 102.83, place: str = "ขอนแก่น",
                 tz: str = "Asia/Bangkok", cache_s: float = 600.0, timeout: float = 15.0):
        self.lat, self.lon, self.place, self.tz = latitude, longitude, place, tz
        self.cache_s, self.timeout = cache_s, timeout
        self._cached: tuple[float, dict] | None = None

    def _get(self, base: str, **params) -> dict:
        q = urllib.parse.urlencode({"latitude": self.lat, "longitude": self.lon, "timezone": self.tz, **params})
        with urllib.request.urlopen(f"{base}?{q}", timeout=self.timeout) as r:
            return json.load(r)

    def now(self) -> dict:
        if self._cached and time.monotonic() - self._cached[0] < self.cache_s:
            return self._cached[1]
        f = self._get("https://api.open-meteo.com/v1/forecast",
                      current="temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
                              "weather_code,wind_speed_10m",
                      daily="temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                      forecast_days=2)
        c, d = f["current"], f["daily"]
        out = {
            "place": self.place,
            "now": {"temp_c": c["temperature_2m"], "feels_like_c": c["apparent_temperature"],
                    "humidity_pct": c["relative_humidity_2m"], "rain_mm": c["precipitation"],
                    "wind_kmh": c["wind_speed_10m"], "sky": CODES.get(c["weather_code"], str(c["weather_code"]))},
            "today": {"max_c": d["temperature_2m_max"][0], "min_c": d["temperature_2m_min"][0],
                      "rain_chance_pct": d["precipitation_probability_max"][0],
                      "sky": CODES.get(d["weather_code"][0], "")},
            "tomorrow": {"max_c": d["temperature_2m_max"][1], "min_c": d["temperature_2m_min"][1],
                         "rain_chance_pct": d["precipitation_probability_max"][1],
                         "sky": CODES.get(d["weather_code"][1], "")},
        }
        try:
            a = self._get("https://air-quality-api.open-meteo.com/v1/air-quality", current="pm2_5,us_aqi")["current"]
            out["air"] = {"pm2_5": a["pm2_5"], "us_aqi": a["us_aqi"]}
        except Exception:  # air quality is a bonus; the forecast alone still answers
            pass
        self._cached = (time.monotonic(), out)
        return out
