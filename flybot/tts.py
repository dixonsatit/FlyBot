"""Thai text-to-speech through a Wayu-TTS server (the voice the KKH interview kiosk uses).

Wayu (``POST /api/speak`` -> base64 WAV, ``GET /api/voices``) runs on CPU inside the
hospital network, so speech never leaves it. The model is CC-BY-NC-4.0 and is not
bundled here: point ``--tts-url`` at a running Wayu (e.g. the kiosk's sidecar on :7860).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import urllib.request
from collections import OrderedDict

log = logging.getLogger(__name__)

_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️]")


class WayuTTS:
    def __init__(self, base_url: str, voice: str | None = None, speed: float = 1.0,
                 timeout: float = 30.0, cache_size: int = 64):
        self.base_url = base_url.rstrip("/")
        self.voice = voice
        self.speed = speed
        self.timeout = timeout
        self._cache: OrderedDict[tuple, bytes] = OrderedDict()  # repeated lines (reminders) render once
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self._names: dict[str, str] | None = None  # voice id or name -> the name /api/speak expects

    def _request(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def voices(self) -> list[dict]:
        data = self._request("/api/voices")
        voices = [{"id": v["id"], "name": v.get("name", v["id"]), "about": v.get("about", "")}
                  for v in data.get("voices", [])]
        self._names = {k: v["name"] for v in voices for k in (v["id"], v["name"])}
        return voices

    def _voice_name(self, voice: str | None) -> str | None:
        """/api/voices lists ids (m_young_clear) but /api/speak takes display names."""
        if not voice:
            return None
        if self._names is None:
            self.voices()
        return self._names.get(voice, voice)

    def speak(self, text: str, voice: str | None = None, speed: float | None = None) -> bytes:
        """WAV bytes for ``text`` (emoji stripped)."""
        text = _EMOJI.sub("", text).strip()
        if not text:
            raise ValueError("nothing to say")
        key = (text, voice or self.voice, speed or self.speed)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        body = {"text": text, "speed": key[2]}
        name = self._voice_name(key[1])
        if name:
            body["voice"] = name
        wav = base64.b64decode(self._request("/api/speak", body)["audio"])
        with self._lock:
            self._cache[key] = wav
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return wav
