"""Places near the robot from Google Maps (Places API (New) Text Search, needs an API key)."""
from __future__ import annotations

import json
import math
import urllib.request

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join(f"places.{f}" for f in (
    "displayName", "formattedAddress", "location", "rating", "userRatingCount", "priceLevel",
    "currentOpeningHours", "nationalPhoneNumber", "googleMapsUri"))


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(a))


class Places:
    def __init__(self, api_key: str, latitude: float, longitude: float, radius_m: float = 5000.0,
                 url: str = URL, timeout: float = 15.0):
        self.key, self.lat, self.lon, self.radius_m = api_key, latitude, longitude, radius_m
        self.url, self.timeout = url, timeout

    def search(self, query: str, open_now: bool = False, limit: int = 5) -> dict:
        body = {"textQuery": query, "languageCode": "th", "regionCode": "TH", "pageSize": max(1, min(limit, 10)),
                "locationBias": {"circle": {"center": {"latitude": self.lat, "longitude": self.lon},
                                            "radius": self.radius_m}}}
        if open_now:
            body["openNow"] = True
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "X-Goog-Api-Key": self.key, "X-Goog-FieldMask": FIELDS})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            places = json.load(r).get("places", [])
        out = []
        for p in places:
            loc = p.get("location", {})
            hours = p.get("currentOpeningHours", {})
            row = {"name": p.get("displayName", {}).get("text", ""), "address": p.get("formattedAddress", ""),
                   "rating": p.get("rating"), "reviews": p.get("userRatingCount"),
                   "open_now": hours.get("openNow"), "phone": p.get("nationalPhoneNumber"),
                   "price": p.get("priceLevel"), "maps": p.get("googleMapsUri")}
            if "latitude" in loc:
                row["distance_km"] = round(distance_km(self.lat, self.lon, loc["latitude"], loc["longitude"]), 1)
            if hours.get("weekdayDescriptions"):
                row["hours"] = hours["weekdayDescriptions"]
            out.append({k: v for k, v in row.items() if v not in (None, "", [])})
        return {"query": query, "places": out}
