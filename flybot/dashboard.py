"""Web dashboard for the bridge: watch the fly brain, chat, and drive a simulated robot.

    python -m flybot.mqtt_bridge ... --dashboard-port 8080 [--dashboard-sim]

One page (``flybot/web/dashboard.html``) polls ``/api/state``: the robot's face, head
pose and balloon text, the circuit activity, events and chat. ``/api/*`` POSTs chat
with the LLM cortex, change personality / game / gaze, and control the simulated world.

With the simulator on, ``flybot.plant`` stands in for the hardware (SG90 lag, the
firmware's frame-differencing camera, latency): a background thread steps it at 100 Hz,
feeds its sensor messages into the bridge and takes the bridge's commands, so the
dashboard behaves like the robot would, before one exists. Turn it off when a real
robot is connected, or both will feed the same brain.

With ``--tts-url`` (a Wayu-TTS server) the page can speak with the server's Thai voices
through ``/api/tts``; otherwise it uses the browser's own voices. With ``--stt-url`` (the
asr-typhoon sidecar) the microphone records 16 kHz PCM that ``/api/stt`` transcribes inside
the network; otherwise the page falls back to the browser's recognizer.

Only the standard library is used. Optional HTTP basic auth (``--dashboard-password``).
"""
from __future__ import annotations

import base64
import collections
import hmac
import json
import logging
import math
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .controller import PERSONALITIES
from .plant import PlantConfig, StackChanPlant, World

log = logging.getLogger(__name__)


# What the robot says when its head is stroked (it calls itself หนู).
PAT_LINES = ("หนูฟินจังเลย", "หนูชอบให้ลูบหัว", "ฟินมากเลย หนูชอบ", "อีกนิดนึงนะ หนูชอบ")


def to_16k(wav: bytes) -> bytes:
    """Resample a mono 16-bit PCM WAV to 16 kHz (Wayu-TTS sends 24 kHz): a third less to send
    over the robot's WiFi. Anything else is returned untouched."""
    import struct

    import numpy as np
    if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        return wav
    fmt, ch, rate, _, _, bits = struct.unpack("<HHIIHH", wav[20:36])
    if fmt != 1 or ch != 1 or bits != 16 or rate <= 16000 or wav[36:40] != b"data":
        return wav
    x = np.frombuffer(wav[44:44 + (len(wav) - 44) // 2 * 2], dtype="<i2").astype(np.float32)
    n = int(len(x) * 16000 / rate)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype("<i2").tobytes()
    return (b"RIFF" + struct.pack("<I", 36 + len(y)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) + b"data" + struct.pack("<I", len(y)) + y)


class SimRobot:
    """Interactive world + simulated StackChan feeding the bridge's sensor state."""

    def __init__(self, bridge, rate_hz: float = 100.0):
        self.bridge = bridge
        self.dt = 1.0 / rate_hz
        self.enabled = False
        self.lock = threading.Lock()
        self.plant = StackChanPlant(PlantConfig())
        self.target_az, self.target_el = 20.0, 10.0
        self.auto = True  # target wanders by itself (frame differencing only sees moving things)
        self._rotate_until = self._pet_until = self._loom_start = -math.inf
        self._t0 = time.monotonic()
        threading.Thread(target=self._run, name="sim", daemon=True).start()

    # -- controls (any thread) -------------------------------------------------
    def set_target(self, az: float, el: float) -> None:
        with self.lock:
            self.target_az = max(-120.0, min(120.0, float(az)))
            self.target_el = max(-20.0, min(40.0, float(el)))
            self.auto = False

    def action(self, name: str) -> None:
        now = time.monotonic()
        with self.lock:
            if name == "rotate":
                self._rotate_until = now + 1.0  # body turned 45 deg clockwise
            elif name == "pet":
                self._pet_until = now + 3.0  # a hand hovering close
            elif name == "loom":
                self._loom_start = now
            elif name == "auto":
                self.auto = True
            else:
                raise ValueError(f"unknown action {name!r}")

    def command(self, cmd: dict) -> None:
        if self.enabled:
            self.plant.command(time.monotonic() - self._t0, cmd)

    # -- simulation thread -------------------------------------------------------
    def _world(self, now: float) -> World:
        t = now - self._t0
        with self.lock:
            if self.auto:
                self.target_az = 20.0 + 30.0 * math.sin(2 * math.pi * 0.15 * t)
                self.target_el = 10.0 + 6.0 * math.sin(2 * math.pi * 0.11 * t)
            loom = now - self._loom_start
            size, distance = 6.0, 1000.0
            if 0 <= loom < 0.9:  # something rushes at the camera
                size = 6.0 * math.exp(2.6 * loom)
                distance = 600.0 * math.exp(-1.8 * loom)
            if now < self._pet_until:
                distance = 80.0
            return World(target_az=self.target_az, target_el=self.target_el, target_size_deg=size,
                         body_yaw_rate=45.0 if now < self._rotate_until else 0.0, distance_mm=distance)

    def _run(self) -> None:
        while True:
            start = time.monotonic()
            if self.enabled:
                try:  # the plant schedules on its own clock, which starts at 0
                    for kind, payload in self.plant.step(start - self._t0, self.dt, self._world(start)):
                        self.bridge.sensors.update(kind, payload, now=time.monotonic())
                except Exception:
                    log.exception("simulator step failed")
            time.sleep(max(0.0, self.dt - (time.monotonic() - start)))

    def state(self) -> dict:
        p = self.plant
        with self.lock:
            return {"enabled": self.enabled, "auto": self.auto,
                    "target": {"az": round(self.target_az, 1), "el": round(self.target_el, 1)},
                    "body_yaw": round(p.body_yaw, 1), "pan": round(p.pan, 1), "tilt": round(p.tilt, 1),
                    "looming": time.monotonic() - self._loom_start < 0.9,
                    "petting": time.monotonic() < self._pet_until}


class Dashboard:
    def __init__(self, bridge, port: int = 8080, user: str = "stackchan", password: str | None = None,
                 sim: bool = False, tts=None, stt=None, wake_name: str = r"^\s*(หวี|วี|wee)(?!ดี|ซ่|ไอ|ร)|น้อง\s*(หวี่|หวี|วี่|วี|v)|หวี",
                 follow_up_s: float = 20.0):
        self.bridge = bridge
        self.wake = re.compile(wake_name, re.IGNORECASE)
        self.follow_up_s = follow_up_s  # after a spoken reply, the next sentence needs no name
        self._last_reply = -math.inf
        self._announcements: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self._announce_seq = 0
        self.tts = tts  # flybot.tts.WayuTTS or None
        self.stt = stt  # flybot.stt.AsrClient or None
        self.user, self.password = user, password
        self.sim = SimRobot(bridge)
        self.sim.enabled = sim
        self.feed: collections.deque = collections.deque(maxlen=80)
        self._feed_id = 0
        self._lock = threading.Lock()
        self.last_cmd: dict = {}
        self.page = resources.files("flybot").joinpath("web/dashboard.html").read_bytes()
        self.server = ThreadingHTTPServer(("0.0.0.0", port), self._handler())
        self.port = self.server.server_port

    def start(self) -> None:
        self._serving = True
        threading.Thread(target=self.server.serve_forever, name="dashboard", daemon=True).start()
        log.info("Dashboard on http://0.0.0.0:%d (simulator %s)", self.port, "on" if self.sim.enabled else "off")

    def stop(self) -> None:
        if getattr(self, "_serving", False):  # shutdown() blocks forever if serve_forever never ran
            self.server.shutdown()
        self.server.server_close()

    # -- hooks from the bridge -----------------------------------------------------
    def on_command(self, cmd: dict) -> None:
        self.last_cmd = cmd
        self.sim.command(cmd)

    def add(self, kind: str, data: dict) -> None:
        with self._lock:
            self._feed_id += 1
            self.feed.append({"id": self._feed_id, "at": time.strftime("%H:%M:%S"), "kind": kind, "data": data})

    # -- API -------------------------------------------------------------------------
    def state(self, since: int = 0) -> dict:
        c = self.bridge.controller
        cmd = self.last_cmd
        with self._lock:
            feed = [f for f in self.feed if f["id"] > since]
        cortex = self.bridge.cortex
        return {
            "servo": cmd.get("servo", {}), "face": cmd.get("face", {}), "text": c.snapshot.get("text", ""),
            "brain": c.snapshot.get("brain", {}), "game": cmd.get("game"),
            "personality": c.personality,
            "personalities": sorted(PERSONALITIES), "game_on": c.cfg.game, "llm": cortex is not None,
            "connected": self.bridge.client.is_connected(), "sim": self.sim.state(), "feed": feed,
            "tts": self.tts is not None, "stt": self.stt is not None,
        }

    def post(self, path: str, body: dict) -> dict:
        c = self.bridge.controller
        if path == "/api/chat":
            text = str(body.get("text", "")).strip()
            if not text:
                raise ValueError("empty message")
            if not self.bridge.cortex:
                raise ValueError("LLM is not enabled (--llm)")
            self.add("you", {"text": text})
            self.bridge.cortex.on_chat(text)
        elif path == "/api/personality":
            name = body.get("name")
            if name not in PERSONALITIES:
                raise ValueError(f"unknown personality {name!r}")
            c.set_personality(name)
        elif path == "/api/game":
            c.configure(game=bool(body.get("on")))
        elif path == "/api/look":
            c.look_at(float(body.get("pan", 0)), float(body.get("tilt", 0)))
        elif path == "/api/say":
            c.say(str(body.get("text", ""))[:40])
        elif path == "/api/sim":
            if "enabled" in body:
                self.sim.enabled = bool(body["enabled"])
            if "target" in body:
                self.sim.set_target(body["target"].get("az", 0), body["target"].get("el", 0))
            if "action" in body:
                self.sim.action(str(body["action"]))
        else:
            raise KeyError(path)
        return {"ok": True}

    def announce(self, text: str) -> bool:
        """Have the robot say ``text`` on its own (a reminder, a CI failure): render it, keep it for
        GET /api/announce/<id>, and tell the robot on ``<base>/announce``."""
        if not self.tts or not text:
            return False
        try:
            wav = to_16k(self.tts.speak(text))
        except Exception as e:
            log.warning("announce: TTS failed: %s", e)
            return False
        with self._lock:
            self._announce_seq += 1
            key = str(self._announce_seq)
            self._announcements[key] = wav
            while len(self._announcements) > 8:
                self._announcements.popitem(last=False)
        self.add("chat", {"type": "announce", "text": text, "voice": True})
        self.bridge.client.publish(f"{self.bridge.base}/announce", json.dumps({"id": key}), qos=1)
        return True

    def _authorized(self, header: str | None) -> bool:
        if not self.password:
            return True
        if not header or not header.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return False
        return hmac.compare_digest(user, self.user) and hmac.compare_digest(pw, self.password)

    def _handler(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode())

            def _auth(self) -> bool:
                if dash._authorized(self.headers.get("Authorization")):
                    return True
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="FlyBot"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False

            def do_GET(self):
                if self.path == "/healthz":
                    return self._send(200, b"ok", "text/plain")
                if not self._auth():
                    return
                if self.path in ("/", "/index.html"):
                    return self._send(200, dash.page, "text/html; charset=utf-8")
                if self.path.startswith("/api/announce/"):
                    with dash._lock:
                        wav = dash._announcements.get(self.path.rsplit("/", 1)[-1])
                    return self._send(200, wav, "audio/wav") if wav else self._json(404, {"error": "gone"})
                if self.path == "/api/tts/voices":
                    if not dash.tts:
                        return self._json(200, {"voices": []})
                    try:
                        return self._json(200, {"voices": dash.tts.voices(), "default": dash.tts.voice})
                    except Exception as e:  # TTS server down: the page falls back to browser voices
                        return self._json(200, {"voices": [], "error": str(e)})
                if self.path.startswith("/api/state"):
                    since = 0
                    if "since=" in self.path:
                        try:
                            since = int(self.path.split("since=")[1].split("&")[0])
                        except ValueError:
                            pass
                    return self._json(200, dash.state(since))
                self._json(404, {"error": "not found"})

            def do_POST(self):
                if not self._auth():
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if self.path == "/api/stt":  # raw PCM 16 kHz mono s16le from the page's recorder
                        if not dash.stt:
                            return self._json(404, {"error": "no ASR server (--stt-url)"})
                        pcm = self.rfile.read(length)
                        try:
                            result = dash.stt.transcribe(pcm)
                        except ValueError:
                            raise
                        except Exception as e:
                            return self._json(502, {"error": f"ASR: {e}"})
                        return self._json(200, {"text": result.get("text", ""), "ms": result.get("ms")})
                    if self.path.split("?")[0] == "/api/voice":  # PCM 16 kHz mono s16le in, WAV out
                        # ?wake=1: hands-free speech, answered only when it calls the robot by name
                        return self._voice(self.rfile.read(length), wake="wake=1" in self.path)
                    if self.path == "/api/pat":  # the robot's head was stroked
                        return self._pat()
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if self.path == "/api/tts":
                        if not dash.tts:
                            return self._json(404, {"error": "no TTS server (--tts-url)"})
                        try:
                            wav = dash.tts.speak(str(body.get("text", "")), body.get("voice") or None)
                        except ValueError:
                            raise
                        except Exception as e:
                            return self._json(502, {"error": f"TTS: {e}"})
                        return self._send(200, wav, "audio/wav")
                    self._json(200, dash.post(self.path, body))
                except KeyError:
                    self._json(404, {"error": "not found"})
                except (ValueError, TypeError, AttributeError) as e:
                    self._json(400, {"error": str(e)})

            def _pat(self):
                dash.bridge.controller.pet()
                if not dash.tts:
                    return self._send(204, b"", "audio/wav")
                try:
                    return self._send(200, dash.tts.speak(random.choice(PAT_LINES)), "audio/wav")
                except Exception as e:
                    return self._json(502, {"error": f"TTS: {e}"})

            def _voice(self, pcm: bytes, wake: bool = False):
                if not (dash.stt and dash.tts and dash.bridge.cortex):
                    return self._json(404, {"error": "voice needs --stt-url, --tts-url and --llm"})
                t0 = time.monotonic()
                try:
                    text = dash.stt.transcribe(pcm).get("text", "").strip()
                except ValueError:
                    raise
                except Exception as e:
                    log.warning("voice: ASR failed (%.1fs, wake=%d): %s", len(pcm) / 32000, wake, e)
                    return self._json(502, {"error": f"ASR: {e}"})
                seconds = len(pcm) / 32000
                follow_up = time.monotonic() - dash._last_reply < dash.follow_up_s
                if not text or (wake and not follow_up and not dash.wake.search(text)):  # not for the robot
                    log.info("voice: %s (%.1fs) -> 204", f"heard {text!r} without the wake name" if text
                             else "nothing understood", seconds)
                    return self._send(204, b"", "audio/wav")
                dash.add("you", {"text": text, "voice": True})
                t_asr = time.monotonic()
                try:
                    reply = dash.bridge.cortex.ask(text)
                    t_llm = time.monotonic()
                    wav = to_16k(dash.tts.speak(reply)) if reply else b""
                except Exception as e:
                    log.warning("voice: %r failed: %s: %s", text, type(e).__name__, e)
                    return self._json(502, {"error": f"{type(e).__name__}: {e}"})
                log.info("voice: %r (%.1fs, wake=%d%s) -> %r | asr %.1fs llm %.1fs tts %.1fs, %d KB", text,
                         seconds, wake, ", follow-up" if follow_up else "", reply, t_asr - t0, t_llm - t_asr,
                         time.monotonic() - t_llm, len(wav) // 1024)
                dash._last_reply = time.monotonic()
                return self._send(200, wav, "audio/wav") if wav else self._send(204, b"", "audio/wav")

        return Handler
