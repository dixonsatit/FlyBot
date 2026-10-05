"""Thai speech-to-text through the hospital's ASR sidecar (the interview kiosk's asr-typhoon).

``POST /api/transcribe`` takes raw PCM, 16 kHz mono s16le, and answers
``{"text", "ms", "speech_ms"}``. The server keeps no audio and logs no text, and it
runs on CPU inside the network, so speech does not leave the hospital.
"""
from __future__ import annotations

import json
import urllib.request

SAMPLE_RATE = 16000
MAX_SECONDS = 60  # the sidecar rejects longer clips


class AsrClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def transcribe(self, pcm: bytes) -> dict:
        if not pcm or len(pcm) % 2 or len(pcm) > MAX_SECONDS * SAMPLE_RATE * 2:
            raise ValueError(f"expected 16-bit PCM, at most {MAX_SECONDS} s at {SAMPLE_RATE} Hz")
        req = urllib.request.Request(self.base_url + "/api/transcribe", data=pcm, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def ready(self) -> bool:
        try:
            with urllib.request.urlopen(self.base_url + "/api/status", timeout=5) as resp:
                return bool(json.loads(resp.read()).get("ready"))
        except Exception:
            return False
