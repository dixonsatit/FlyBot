"""History taking (ซักประวัติ) through the hospital's interview server (stackchan-interview).

The robot becomes one more thin client of that server, which owns the whole conversation: its
fixed questions, red-flag stop, extraction and the draft for the nurse (docs/SPEC.md there). The
bridge only relays, over the server's WebSocket protocol:

- a sentence the robot recorded -> 20 ms PCM frames + ``mic_close`` (the server transcribes it
  with the hospital's own ASR), sent only while the server says ``listening`` (half-duplex);
- each ``say`` -> its audio (the server's pre-rendered ``/audio/<id>``, or our TTS when it asks
  the device to speak) played on the robot, then ``played`` once it has had time to finish.

A patient can log in with ThaID first (``Pairing``): the server hands out a one-time code, the
robot shows it as a QR, the patient logs in on their own phone, and the robot's connection then
carries that login (``/ws?pair=``). The phone never gets the login, and the code works once.

Nothing of the patient's goes into the assistant: no LLM of ours, no memory, no audit log, no
voice archive. What the dashboard shows is held in memory only.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable

log = logging.getLogger(__name__)

FRAME = 640  # bytes: 20 ms of 16 kHz mono s16le (AUDIO_FRAME_BYTES in the server)
DIALECTS = ("th", "isan")


class IntakeSession:
    """One interview with one patient, from ``start`` to ``done`` (or cancel)."""

    def __init__(self, ws_url: str, play: Callable[[bytes, str], float], speak: Callable[[str], bytes] | None,
                 on_event: Callable[[dict], None], dialect: str = "th", fetch_timeout: float = 10.0,
                 connect: Callable | None = None, pair: str | None = None, token: str | None = None):
        self.ws_url = ws_url.rstrip("/")
        q = {"dialect": dialect if dialect in DIALECTS else "th"}
        if pair:
            q["pair"] = pair
        self.url = f"{self.ws_url}?{urllib.parse.urlencode(q)}"
        self.headers = {"Authorization": f"Bearer {token}"} if pair and token else {}
        self.paired = bool(pair)
        self.audio_base = http_base(self.ws_url)
        self.play, self.speak, self.on_event = play, speak, on_event
        self.fetch_timeout = fetch_timeout
        self._connect = connect
        self.state, self.mic = "idle", False
        self.slots: list = []
        self.done: dict | None = None
        self.alert: dict | None = None
        self.closed = False
        self._ws = None
        self._send_lock = threading.Lock()
        self._played_timer: threading.Timer | None = None

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        if self._connect is None:
            from websockets.sync.client import connect
            self._connect = connect
        try:  # websockets >= 15 wants a context manager unless legacy=True (this connection outlives any block)
            self._ws = self._connect(self.url, open_timeout=10, max_size=2 ** 22, legacy=True,
                                     additional_headers=self.headers)
        except TypeError:
            self._ws = self._connect(self.url, open_timeout=10, max_size=2 ** 22, additional_headers=self.headers)
        self._send({"t": "hello", "fw": "flybot-bridge", "caps": ["server_asr"]})
        self._send({"t": "start"})
        threading.Thread(target=self._read, name="intake", daemon=True).start()
        self.on_event({"type": "intake", "phase": "started", "paired": self.paired})

    def cancel(self) -> None:
        """The patient (or staff) stops: the server drops what it had, no draft."""
        if self.closed:
            return
        try:
            self._send({"t": "cancel"})
        except Exception:
            pass
        self.close()
        self.on_event({"type": "intake", "phase": "cancelled"})

    def close(self) -> None:
        self.closed = True
        if self._played_timer:
            self._played_timer.cancel()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # -- robot -> server ---------------------------------------------------------------
    def heard(self, pcm: bytes) -> bool:
        """A sentence the robot recorded. Sent only while the server listens; otherwise it is
        dropped (sending it would break the server's half-duplex rule)."""
        if self.closed or self.state != "listening" or not self.mic:
            log.info("intake: dropped a sentence while the server is %s", self.state)
            return False
        pcm = pcm[: len(pcm) - len(pcm) % 2]
        with self._send_lock:
            for i in range(0, len(pcm), FRAME):
                self._ws.send(pcm[i:i + FRAME])
            self._ws.send(json.dumps({"t": "mic_close", "reason": "endpoint"}))
        return True

    def _send(self, msg: dict) -> None:
        with self._send_lock:
            self._ws.send(json.dumps(msg, ensure_ascii=False))

    # -- server -> robot ---------------------------------------------------------------
    def _read(self) -> None:
        try:
            for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                self.handle(json.loads(raw))
        except Exception as e:
            if not self.closed:
                log.warning("intake: connection lost: %s", e)
                self.on_event({"type": "intake", "phase": "error", "detail": str(e)})
        finally:
            self.closed = True

    def handle(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "state":
            self.state, self.mic = msg.get("v", self.state), bool(msg.get("mic"))
            self.on_event({"type": "intake", "phase": "state", "state": self.state})
        elif t in ("say", "announce"):
            self._say(msg)
        elif t == "slots":
            self.slots = msg.get("v", [])
            self.on_event({"type": "intake", "phase": "slots", "slots": self.slots, "current": msg.get("current")})
        elif t == "heard":
            self.on_event({"type": "intake", "phase": "heard", "text": msg.get("v", "")})
        elif t == "alert":
            self.alert = msg
            self.on_event({"type": "intake", "phase": "alert", "level": msg.get("level"),
                           "labels": msg.get("labels", []), "detail": msg.get("detail", "")})
        elif t == "done":
            self.done = msg.get("summary", {})
            self.on_event({"type": "intake", "phase": "done", "status": self.done.get("status"),
                           "conflicts": len(self.done.get("conflicts", [])),
                           "red_flag": bool(self.done.get("red_flag"))})
            self.close()
        elif t == "error":
            log.warning("intake: server error %s: %s", msg.get("code"), msg.get("detail"))
            self.on_event({"type": "intake", "phase": "error", "code": msg.get("code"), "detail": msg.get("detail")})
        elif t == "stop_audio" and self._played_timer:
            self._played_timer.cancel()

    def _say(self, msg: dict) -> None:
        sid, text, mode = msg.get("id", ""), msg.get("text", ""), msg.get("audio", "client")
        self.on_event({"type": "intake", "phase": "say", "text": text})
        wav = b""
        try:
            if mode == "server":
                with urllib.request.urlopen(f"{self.audio_base}/audio/{urllib.parse.quote(sid)}",
                                            timeout=self.fetch_timeout) as r:
                    wav = r.read()
            elif mode == "client" and self.speak:
                wav = self.speak(text)
        except Exception as e:  # no sound: the text is still on the screens, and the server goes on
            log.warning("intake: no audio for %s: %s", sid, e)
        seconds = self.play(wav, text) if wav else 0.0

        def played():
            if not self.closed:
                try:
                    self._send({"t": "played", "id": sid})
                except Exception as e:
                    log.warning("intake: could not send played: %s", e)
        # the robot gives no "finished playing" signal: allow its fetch and the clip itself
        self._played_timer = threading.Timer(seconds + (1.2 if wav else 0.3), played)
        self._played_timer.daemon = True
        self._played_timer.start()


def http_base(ws_url: str) -> str:
    """The interview server's HTTP root from its WebSocket URL."""
    return ws_url.replace("ws://", "http://", 1).replace("wss://", "https://", 1).rstrip("/").rsplit("/ws", 1)[0]


class Pairing:
    """Waits for the patient to log in with ThaID on their phone after scanning the robot's QR.

    ``start()`` asks the server for a code (``POST /api/pair``) and returns the URL to show; a thread
    then polls the code's state and calls ``on_bound(code)`` once the login is tied to it, or
    ``on_expired()`` when nobody logged in in time. Only the bridge holds the token that makes and
    redeems codes, so a photo of the QR alone can't take the login.
    """

    def __init__(self, ws_url: str, token: str, public_url: str, on_bound: Callable[[str], None],
                 on_expired: Callable[[], None], poll_s: float = 1.0, timeout: float = 10.0):
        self.base, self.token = http_base(ws_url.rstrip("/")), token
        self.public_url = public_url.rstrip("/")
        self.on_bound, self.on_expired = on_bound, on_expired
        self.poll_s, self.timeout = poll_s, timeout
        self.code: str | None = None
        self.expires = 0.0
        self.done = False

    def _call(self, method: str, path: str) -> dict:
        req = urllib.request.Request(f"{self.base}{path}", method=method,
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def start(self) -> str:
        got = self._call("POST", "/api/pair")
        self.code = got["code"]
        self.expires = time.monotonic() + float(got.get("expires_in", 180))
        threading.Thread(target=self._wait, name="intake-pair", daemon=True).start()
        return f"{self.public_url}{got['path']}"

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.expires - time.monotonic())

    def cancel(self) -> None:
        self.done = True

    def _wait(self) -> None:
        while not self.done:
            time.sleep(self.poll_s)
            if self.done:
                return
            try:
                state = self._call("GET", f"/api/pair/{self.code}").get("state")
            except Exception as e:  # a blip: keep waiting until the code's own deadline
                log.warning("intake: pairing poll failed: %s", e)
                state = "expired" if self.seconds_left <= 0 else "pending"
            if state == "pending" and self.seconds_left > 0:
                continue
            self.done = True
            if state == "bound":
                self.on_bound(self.code)
            else:
                self.on_expired()


def wav_seconds(wav: bytes) -> float:
    """Length of a PCM WAV from its header (16 kHz mono 16-bit when unsure)."""
    import struct
    try:
        rate, = struct.unpack_from("<I", wav, 24)
        block, = struct.unpack_from("<H", wav, 32)
        return max(0.0, (len(wav) - 44) / (rate * block))
    except struct.error:
        return len(wav) / 32000
