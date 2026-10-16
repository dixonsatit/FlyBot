"""Thai speech-to-text through the hospital's ASR sidecar (the interview kiosk's asr-typhoon).

``POST /api/transcribe`` takes raw PCM, 16 kHz mono s16le, and answers
``{"text", "ms", "speech_ms"}``. The server keeps no audio and logs no text, and it
runs on CPU inside the network, so speech does not leave the hospital.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
MAX_SECONDS = 60  # the sidecar rejects longer clips

# Whisper can get stuck repeating one phrase to the end of its window ("มันอร่อย มันอร่อย ..." x100).
_LOOP = re.compile(r"(\S.{1,29}?)(?:\s*\1){3,}(\s*\S*)")


def unloop(text: str) -> str:
    """Collapse a phrase said 4+ times in a row into one (a Whisper loop, not speech)."""
    # the loop is usually cut mid-phrase at the end of the window ("... มันอร่อย มันอ"): drop that stub too
    return _LOOP.sub(lambda m: m[1] + ("" if m[1].startswith(m[2].strip()) else m[2]), text).strip()


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
            result = json.loads(resp.read())
        result["text"] = unloop(result.get("text") or "")
        return result

    def ready(self) -> bool:
        try:
            with urllib.request.urlopen(self.base_url + "/api/status", timeout=5) as resp:
                return bool(json.loads(resp.read()).get("ready"))
        except Exception:
            return False


class FallbackAsr:
    """Ask ``primary`` first and ``fallback`` only when it fails (down, timeout, error).

    An empty answer is kept: both servers run a VAD, so empty means no speech, and asking
    the second one would only add latency.
    """

    def __init__(self, primary: AsrClient, fallback: AsrClient):
        self.primary, self.fallback = primary, fallback

    def transcribe(self, pcm: bytes) -> dict:
        try:
            return self.primary.transcribe(pcm)
        except ValueError:
            raise
        except Exception as e:
            log.warning("ASR %s failed (%s), using %s", self.primary.base_url, e, self.fallback.base_url)
            return self.fallback.transcribe(pcm)

    def ready(self) -> bool:
        return self.primary.ready() or self.fallback.ready()
