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
import contextlib
import hmac
import json
import logging
import math
import os
import random
import re
import threading
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .controller import PERSONALITIES
from .plant import PlantConfig, StackChanPlant, World

log = logging.getLogger(__name__)


# Words that only make sense said to the robot (its eyes, its head, "you"), for when the name was lost.
ADDRESSED_RE = re.compile(r"เห็น(ผม|ฉัน|หนู|ไหม)|ได้ยิน(ผม|ไหม)|หัน(หน้า|มา|ซ้าย|ขวา|ไป)|มองมา|เงย|ก้ม|พยักหน้า|"
                          r"ส่ายหน้า|หนู(ช่วย|ทำ|รู้|จำ|จด|เตือน)|ช่วย(บอก|ดู|เตือน|จำ|จด|เปิด|หา)|ตอบ(ผม|หน่อย)")
CALL_MAX_CHARS = 4  # a hands-free utterance this short is taken as the robot being called
CALL_REPLY = "ว่าไงครับ"
# "ซักประวัติ" said to the robot starts history taking (no name needed, no LLM in the way), and
# "หยุด/ยกเลิกการซักประวัติ" heard during it cancels without a draft.
INTAKE_START_RE = re.compile(r"(เริ่ม|ช่วย|ขอ|อยาก)?\s*ซัก\s*ประวัติ")
INTAKE_STOP_RE = re.compile(r"(หยุด|ยกเลิก|เลิก|พอ)(การ)?\s*ซัก\s*ประวัติ|ไม่\s*ซัก(แล้ว|ต่อ)")
# Interview without logging in, said either with the request or while the QR is up.
INTAKE_SKIP_RE = re.compile(r"ซัก\s*เลย|ไม่\s*(ต้อง\s*)?(login|ล[็๊]?อ[็๊]?ก\s*อิน|ยืนยัน\s*ตัว\s*ตน)|ข้าม")
PAIR_QR_TEXT = "สแกนเพื่อยืนยันตัวตน ThaID"
PAIR_LINE = ("ถ้าจะยืนยันตัวตน สแกนคิวอาร์บนจอด้วยมือถือ แล้วล็อกอินไทดีครับ "
             "ถ้าไม่ล็อกอิน พูดว่า ซักเลย ได้เลยครับ")
PAIR_EXPIRED_LINE = "คิวอาร์หมดเวลาแล้วครับ ถ้ายังจะซักประวัติ เรียกหนูใหม่ได้เลยครับ"

# What the robot says when its head is stroked (it calls itself หนู).
# Lines the robot keeps and plays itself: while a voice reply is being made (Dashboard.progress),
# and at once when it is lifted, tilted or shaken (firmware motion.cpp picks one per kind).
FILLERS = {
    "search": "กำลังค้นหาข้อมูลให้นะครับ",
    "wait": "รอแป๊บนึงนะครับ ยังหาอยู่ครับ",
    "lift1": "เฮ้ย! ระวังหนูหน่อยนะครับ เดี๋ยวหล่น!",
    "lift2": "อุ๊ย จะพาหนูไปไหนครับเนี่ย",
    "lift3": "ว้าย ตกใจหมดเลย วางหนูเบาๆ นะครับ",
    "tilt1": "เอียงแล้วๆ หนูจะล้มแล้วครับ!",
    "tilt2": "โอ๊ะ เวียนหัวแล้วครับ วางตรงๆ หน่อย",
    "shake1": "หยุดเขย่าก่อนครับ หนูมึนแล้ว!",
    "shake2": "โอ๊ยยย อย่าเขย่าหนูสิครับ",
}

# What the robot itself calls, with its own (MQTT) login
ROBOT_PATHS = ("/api/voice", "/api/pat", "/api/announce/", "/api/filler/")
MAX_FAILURES, LOCKOUT_S = 8, 300.0

PAT_LINES = (
    "โอยยยย ฟินจังเอาอีกๆ",
    "อุ๊ยยย ตรงนั้นแหละ ฟินมาก",
    "งื้ออออ ชอบจัง ลูบอีกนะ",
    "โอ๊ยยย ละลายแล้ว เอาอีกๆ",
    "ฮึ่ยยย ฟินจนแบตจะเต็ม",
    "อ๊ากกก ดีจังเลย อย่าเพิ่งหยุดนะ",
    "หนูฟินจนลืมโค้ดหมดแล้ว เอาอีก",
    "อืออออ ตรงนี้ๆ อีกนิดนึง",
)


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
                 sim: bool = False, tts=None, stt=None, wake_name: str = r"^\s*(สวัสดี|หวัดดี|ฮัลโหล|ฮัลโล|เฮ้|hello|hi\b|hey)|^\s*(หวี|วี|wee)(?!ดี|ซ่|ไอ|ร)|น้อง\s*(หวี่|หวี|วี่|วี|v)|หวี",
                 follow_up_s: float = 20.0):
        self.bridge = bridge
        self.wake = re.compile(wake_name, re.IGNORECASE)
        self.follow_up_s = follow_up_s  # after a spoken reply, the next sentence needs no name
        self._last_reply = -math.inf
        self._announcements: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self.held: list[str] = []  # announcements kept while nobody was at the desk
        self.archive_dir: str | None = None  # set to keep recent voice clips (see archive_clip)
        # Wake-word recording session (POST /api/wakeword): every hands-free clip is kept as a
        # training sample under <archive_dir>/../wakeword/<kind>/ instead of being answered.
        self.collect: dict | None = None
        self.faces = None  # (FaceClient, FaceRegistry, StaffDirectory | None) when --face-url is set
        self.tracker = None  # flybot.faces.FaceTracker: paused while a person steers the head
        self.memory = None  # (flybot.memory.Episodes, flybot.memory.Audit) when the assistant keeps memory
        self.intake_url: str | None = None  # the interview server's /ws (stackchan-interview), --intake-url
        self.intake = None  # flybot.intake.IntakeSession while history taking runs
        self.intake_view: dict = {}  # what the page shows of it (memory only, gone with the session)
        # ThaID login paired by a QR on the robot (the interview server's PAIR_TOKEN and public URL)
        self.intake_pair_token: str | None = None
        self.intake_public_url: str | None = None
        self.pairing = None  # flybot.intake.Pairing while the QR waits for the patient's phone
        self.pair_poll_s = 1.0
        self._announce_seq = 0
        self._fillers: dict[str, bytes] = {}
        self.tts = tts  # flybot.tts.WayuTTS or None
        self.stt = stt  # flybot.stt.AsrClient or None
        self.user, self.password = user, password
        # The robot's own login (its MQTT user/password) is good only for what the robot calls
        # (ROBOT_PATHS): a password read out of its flash does not open the dashboard.
        self.robot_user, self.robot_password = "stackchan", None
        self._failures: dict[str, list[float]] = {}  # client -> times of recent wrong passwords
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
            if body.get("speak"):  # the robot says the reply aloud too
                self.bridge.cortex.on_chat(text, then=lambda reply: reply and self.announce(reply, force=True))
            else:
                self.bridge.cortex.on_chat(text)
        elif path == "/api/personality":
            name = body.get("name")
            if name not in PERSONALITIES:
                raise ValueError(f"unknown personality {name!r}")
            c.set_personality(name)
        elif path == "/api/game":
            c.configure(game=bool(body.get("on")))
        elif path == "/api/wakeword":
            if body.get("stop"):
                self.collect = None
            else:
                kind = body.get("kind", "positive")
                if kind not in ("positive", "negative") or not self.archive_dir:
                    raise ValueError("kind must be positive or negative, and the bridge needs --state-dir")
                self.collect = {"kind": kind, "left": max(1, min(int(body.get("count", 50)), 500)), "saved": 0}
            log.info("wake word recording: %s", self.collect)
        elif path == "/api/intake":
            if body.get("stop"):
                self.cancel_pairing()
                if self.intake_active():
                    self.intake.cancel()
                return {"ok": True}
            if body.get("login") == "thaid":
                return self.start_pairing(str(body.get("dialect", "th")))
            return self.start_intake(str(body.get("dialect", "th")))
        elif path == "/api/speak":  # say this through the robot's speaker now, even if nobody is there
            text = str(body.get("text", "")).strip()[:300]
            if not text or not self.announce(text, force=True):
                raise ValueError("nothing to say, or no TTS")
        elif path == "/api/episodes/forget":
            if not self.memory:
                raise ValueError("long-term memory is off")
            return {"ok": self.memory[0].forget(int(body.get("id", 0)))}
        elif path == "/api/notes/forget":
            return {"ok": self._store().forget(int(body.get("id", 0)))}
        elif path == "/api/reminders/cancel":
            return {"ok": self._store().cancel(int(body.get("id", 0)))}
        elif path == "/api/people/enroll":
            return self.enroll(body)
        elif path == "/api/people/identify":  # a frame from the browser's camera: who is in it?
            return self.identify(base64.b64decode(str(body.get("image", "")).split(",")[-1]))
        elif path == "/api/people/forget":
            if not self.faces:
                raise ValueError("face recognition is off (--face-url)")
            return {"ok": self.faces[1].forget(str(body.get("id", "")))}
        elif path == "/api/face":  # absolute pose, held so the reflexes leave it there a while
            if self.tracker:
                self.tracker.pause(20.0)
            c.face(float(body.get("pan", 0)), float(body.get("tilt", 0)), hold_s=20.0)
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

    def announce(self, text: str, force: bool = False) -> bool:
        """Have the robot say ``text`` on its own (a reminder, a CI failure): render it, keep it for
        GET /api/announce/<id>, and tell the robot on ``<base>/announce``. While presence says
        nobody is at the desk it is held, and said when they come back."""
        if not self.tts or not text:
            return False
        presence = getattr(self.bridge, "presence", None)
        if presence and not presence.present and not force:
            with self._lock:
                self.held.append(text)
                del self.held[:-10]
            log.info("announce held (nobody here): %s", text)
            return True
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

    def play_wav(self, wav: bytes, text: str = "") -> float:
        """Have the robot play this clip now (as an announcement); returns its length in seconds."""
        from .intake import wav_seconds
        wav = to_16k(wav)
        with self._lock:
            self._announce_seq += 1
            key = str(self._announce_seq)
            self._announcements[key] = wav
            while len(self._announcements) > 8:
                self._announcements.popitem(last=False)
        self.bridge.client.publish(f"{self.bridge.base}/announce", json.dumps({"id": key}), qos=1)
        return wav_seconds(wav)

    def intake_active(self) -> bool:
        return bool(self.intake and not self.intake.closed)

    def pairing_on(self) -> bool:
        return bool(self.intake_url and self.intake_pair_token and self.intake_public_url)

    def pairing_active(self) -> bool:
        return bool(self.pairing and not self.pairing.done)

    def show_qr(self, url: str, text: str = "", seconds: float = 180) -> None:
        from .thai_text import shape
        self.bridge.client.publish(f"{self.bridge.base}/screen/qr", json.dumps(
            {"url": url, "text": shape(text), "seconds": round(seconds)}, ensure_ascii=False))

    def start_pairing(self, dialect: str = "th") -> dict:
        """Show a QR so the patient logs in with ThaID on their phone; the interview starts with that
        login once they have. Saying "ซักเลย" meanwhile starts it without a login."""
        from .intake import Pairing
        if not self.pairing_on():
            raise ValueError("ThaID pairing is off (--intake-pair-token, --intake-public-url)")
        if self.intake_active() or self.pairing_active():
            raise ValueError("an interview is already running")

        def bound(code: str) -> None:
            self.show_qr("")
            log.info("intake: ThaID login paired")
            try:
                self.start_intake(dialect, pair=code)
            except Exception as e:
                log.warning("intake: could not start the paired interview: %s", e)
                self.intake_view["phase"] = "error"

        def expired() -> None:
            self.show_qr("")
            self.intake_view["phase"] = "pair-expired"
            self.add("event", {"type": "intake", "phase": "pair-expired"})
            self.announce(PAIR_EXPIRED_LINE, force=True)

        pairing = Pairing(self.intake_url, self.intake_pair_token, self.intake_public_url, bound, expired,
                          poll_s=self.pair_poll_s)
        url = pairing.start()
        self.pairing = pairing
        self.intake_view = {"dialect": dialect, "phase": "pairing", "lines": [], "slots": [], "state": "idle"}
        self.show_qr(url, PAIR_QR_TEXT, pairing.seconds_left)
        self.add("event", {"type": "intake", "phase": "pairing"})
        self.announce(PAIR_LINE, force=True)
        return {"ok": True, "pairing": True}

    def cancel_pairing(self) -> bool:
        if not self.pairing_active():
            return False
        self.pairing.cancel()
        self.show_qr("")
        self.intake_view["phase"] = "cancelled"
        return True

    def start_intake(self, dialect: str = "th", pair: str | None = None) -> dict:
        """History taking with the patient in front of the robot, through the interview server."""
        from .intake import IntakeSession
        if not self.intake_url:
            raise ValueError("history taking is off (--intake-url)")
        if self.intake_active():
            raise ValueError("an interview is already running")
        if not pair:
            self.cancel_pairing()
        self.intake_view = {"dialect": dialect, "phase": "starting", "lines": [], "slots": [], "state": "idle",
                            "paired": bool(pair)}

        def on_event(ev: dict) -> None:
            v, phase = self.intake_view, ev.get("phase")
            if phase == "heard" and INTAKE_STOP_RE.search(ev.get("text", "")):
                threading.Thread(target=lambda: self.intake and self.intake.cancel(), daemon=True).start()
            if phase in ("say", "heard"):
                v["lines"] = (v.get("lines", []) + [{"who": "robot" if phase == "say" else "patient", "text": ev.get("text", "")}])[-40:]
            elif phase == "slots":
                v["slots"], v["current"] = ev.get("slots", []), ev.get("current")
            elif phase == "state":
                v["state"] = ev.get("state")
            elif phase == "alert":
                v["alert"] = {k: ev.get(k) for k in ("level", "labels", "detail")}
                if hasattr(self.bridge, "leds"):
                    self.bridge.leds((255, 0, 0), "blink", 60)  # red: get a nurse
            elif phase == "done":
                v["done"] = {k: ev.get(k) for k in ("status", "conflicts", "red_flag")}
                if hasattr(self.bridge, "leds"):
                    self.bridge.leds((0, 200, 60), "solid", 8)
            if phase in ("started", "done", "cancelled", "error", "alert"):
                v["phase"] = phase
                self.add("event", {"type": "intake", "phase": phase})  # no patient words in the feed
                log.info("intake: %s", phase)
        session = IntakeSession(self.intake_url, self.play_wav, self.tts.speak if self.tts else None, on_event, dialect,
                                pair=pair, token=self.intake_pair_token)
        session.start()
        self.intake = session
        return {"ok": True}

    def filler(self, name: str) -> bytes | None:
        """A short line the robot keeps and plays while it waits for a reply (FILLERS)."""
        if name not in FILLERS or not self.tts:
            return None
        if name not in self._fillers:
            self._fillers[name] = to_16k(self.tts.speak(FILLERS[name]))
        return self._fillers[name]

    @contextlib.contextmanager
    def progress(self, first_wait_s: float = 8.0, every_s: float = 15.0):
        """While a voice reply is being made, tell the robot on ``<base>/voice/progress`` to play a
        filler: "search" once when a lookup starts (the yielded function), and "wait" after
        ``first_wait_s`` and then every ``every_s`` until the reply is ready."""
        stop = threading.Event()
        sent: set[str] = set()

        def say(name: str) -> None:
            if name == "search" and name in sent:
                return
            sent.add(name)
            self.bridge.client.publish(f"{self.bridge.base}/voice/progress", json.dumps({"say": name}), qos=0)

        def waiting():
            if not stop.wait(first_wait_s):
                say("wait")
                while not stop.wait(every_s):
                    say("wait")

        threading.Thread(target=waiting, name="voice-progress", daemon=True).start()
        try:
            yield say
        finally:
            stop.set()

    def _store(self):
        store = getattr(self.bridge.cortex, "store", None) if self.bridge.cortex else None
        if not store:
            raise ValueError("assistant memory is off (--state-dir)")
        return store

    def overview(self) -> dict:
        """Everything the status bar and the status page show, in one call."""
        b, c = self.bridge, self.bridge.controller
        cortex, presence, greeter = b.cortex, getattr(b, "presence", None), getattr(b, "greeter", None)
        seen = getattr(b, "robot_seen", 0.0)
        who = greeter.who() if greeter else None
        llm = getattr(cortex, "llm", None)
        store = getattr(cortex, "store", None) if cortex else None
        return {
            "now": time.time(),
            "robot": {"online": bool(seen) and time.time() - seen < 15, "seen_ago": round(time.time() - seen) if seen else None,
                      "power": getattr(b, "power", {}), "selftest": getattr(b, "selftest", {}),
                      "pan": round(c.pan, 1), "tilt": round(c.tilt, 1), "expression": c.mb.expression},
            "broker": b.client.is_connected(),
            "presence": {"on": presence is not None, "present": bool(presence and presence.present),
                         "last_seen": presence.last_seen if presence and presence.last_seen > 0 else None},
            "faces": {"on": bool(self.faces), "people": len(self.faces[1].people) if self.faces else 0,
                      "who": who and {k: who.get(k) for k in ("name", "nick", "position")},
                      "tracking": bool(self.tracker and time.monotonic() - self.tracker.last_face < 5)},
            "llm": {"on": cortex is not None, "model": getattr(llm, "model", None),
                    "kind": type(llm).__name__ if llm else None},
            "voice": {"stt": self.stt is not None, "tts": self.tts is not None, "wake": self.wake.pattern,
                      "follow_up_s": self.follow_up_s, "recording": self.collect},
            "assistant": {"notes": len(store.notes) if store else 0, "reminders": len(store.reminders) if store else 0},
            "calendar": bool(cortex and getattr(cortex, "calendar", None)),
            "github": bool(cortex and getattr(cortex, "github", None)),
            "personality": c.personality, "personalities": sorted(PERSONALITIES),
        }

    def voice_log(self, n: int = 40) -> list[dict]:
        """The latest voice turns with what happened to each (newest first); clips that still exist
        can be played from /api/voice/clip/<name>."""
        if not self.archive_dir:
            return []
        try:
            with open(os.path.join(self.archive_dir, "log.jsonl"), encoding="utf-8") as f:
                rows = [json.loads(line) for line in f.readlines()[-n:]]
        except (OSError, ValueError):
            return []
        for r in rows:
            r["playable"] = os.path.exists(os.path.join(self.archive_dir, r.get("clip", "")))
        return rows[::-1]

    def enroll(self, body: dict, gap_s: float = 0.8) -> dict:
        """Enroll a person from webcam pictures (JPEG, base64) taken on the dashboard, or with
        {"source": "robot"} from 5 robot camera frames: the largest face in each. Needs their
        consent (PDPA s.26: a face template is sensitive data)."""
        if not self.faces:
            raise ValueError("face recognition is off (--face-url)")
        if body.get("consent") is not True:
            raise ValueError("the person's consent is required")
        client, registry, staff = self.faces
        info = {k: str(body.get(k, "")).strip() for k in ("name", "nick", "position", "office")}
        code = str(body.get("code", "")).strip()
        if code and staff:
            row = staff.get(int(code))
            if not row:
                raise ValueError(f"no active staff with code {code}")
            info = {"name": f"{row['name']} {row['surname']}", "nick": row.get("nicname") or "",
                    "position": row.get("position") or "", "office": row.get("office_name") or ""}
        if not info["name"]:
            raise ValueError("name (or a staff code) is required")
        images = body.get("images") or []
        if body.get("source") == "robot":
            for _ in range(5):
                jpeg = self.bridge.snapshot(8.0)
                if jpeg:
                    images.append(base64.b64encode(jpeg).decode())
                time.sleep(gap_s)
        if not images:
            raise ValueError("no pictures: take them with the webcam, or the robot is offline")
        embeddings = []
        for b64 in images[:10]:
            found = client.embed(base64.b64decode(str(b64).split(",")[-1]))
            if found:
                embeddings.append(found[0]["embedding"])
        if len(embeddings) < 3:
            raise ValueError(f"saw a face in only {len(embeddings)} of the frames; face the robot and try again")
        person = registry.enroll(code or info["name"], {**info, "code": code}, embeddings)
        log.info("enrolled %s (%d samples)", person.get("name"), person["samples"])
        self.add("event", {"type": "enrolled", "name": person.get("name")})
        return {"ok": True, "person": person}

    def identify(self, jpeg: bytes) -> dict:
        """Every face in one picture, with the enrolled person it matches (or none)."""
        if not self.faces:
            raise ValueError("face recognition is off (--face-url)")
        if not jpeg:
            raise ValueError("no picture")
        client, registry, _ = self.faces
        out = []
        for f in client.embed(jpeg):
            pid, score = registry.match(f["embedding"])
            who = registry.summary(pid) if pid else None
            log.info("identify (webcam): %s cosine %.3f", who.get("name") if who else "unknown", score)
            out.append({"box": f["box"], "score": round(score, 3),
                        "name": who and who.get("name"), "nick": who and who.get("nick")})
        return {"ok": True, "faces": out}

    def keep_wake_sample(self, pcm: bytes) -> str:
        """Save one clip of a wake-word recording session and say what comes next."""
        c = self.collect
        folder = os.path.join(os.path.dirname(self.archive_dir.rstrip("/")), "wakeword", c["kind"])
        os.makedirs(folder, exist_ok=True)
        with wave.open(os.path.join(folder, f"{time.strftime('%Y%m%d-%H%M%S')}-{c['saved']:03d}.wav"), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
            w.writeframes(pcm)
        c["saved"] += 1
        c["left"] -= 1
        log.info("wake word sample %s #%d (%.1fs), %d to go", c["kind"], c["saved"], len(pcm) / 32000, c["left"])
        if c["left"] <= 0:
            self.collect = None
            return "ครบแล้วครับ ขอบคุณมากครับ"
        return f"อีก {c['left']} ครั้งครับ" if c["left"] % 10 == 0 else "ต่อเลยครับ"

    def archive_clip(self, pcm: bytes, info: dict) -> None:
        """Keep the last 30 voice clips with what happened to them (``<archive_dir>/``), so a turn
        that got no answer can be listened to and traced afterwards."""
        if not self.archive_dir:
            return
        import os
        import struct
        try:
            os.makedirs(self.archive_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            header = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " +
                      struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) + b"data" + struct.pack("<I", len(pcm)))
            with open(os.path.join(self.archive_dir, f"{stamp}.wav"), "wb") as f:
                f.write(header + pcm)
            with open(os.path.join(self.archive_dir, "log.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"clip": f"{stamp}.wav", **info}, ensure_ascii=False) + "\n")
            clips = sorted(n for n in os.listdir(self.archive_dir) if n.endswith(".wav"))
            for old in clips[:-30]:
                os.remove(os.path.join(self.archive_dir, old))
        except OSError as e:
            log.warning("voice archive: %s", e)

    def welcome_back(self, left_at: float, now: float, meetings=()) -> str:
        """What to say when the user returns: a meeting they were away for, then what was held."""
        from datetime import datetime
        parts = []
        missed = [m for m in meetings if left_at - 600 <= m.start.timestamp() <= now and now - m.start.timestamp() <= 7200]
        if missed:
            parts.append(f"กลับมาแล้ว ประชุม{missed[-1].title[:40]} เป็นยังไงบ้างครับ")
        with self._lock:
            held, self.held = self.held, []
        if held:
            parts.append("ระหว่างที่ไม่อยู่ " + " ".join(held[-3:]))
        if not parts:
            return ""
        self._last_reply = time.monotonic()  # they can answer without the wake name
        return " ".join(parts)

    def _authorized(self, header: str | None, path: str = "/") -> bool:
        if not self.password:
            return True
        if not header or not header.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return False
        if hmac.compare_digest(user, self.user) and hmac.compare_digest(pw, self.password):
            return True
        return bool(self.robot_password and path.split("?")[0].startswith(ROBOT_PATHS)
                    and hmac.compare_digest(user, self.robot_user) and hmac.compare_digest(pw, self.robot_password))

    def locked_out(self, client: str, failed: bool | None = None) -> bool:
        """Wrong passwords per client: MAX_FAILURES within LOCKOUT_S locks that client out for the
        rest of it. ``failed`` records one more (True) or clears them after a good login (False)."""
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < LOCKOUT_S]
            if failed:
                recent.append(now)
            if failed is False or not recent:
                self._failures.pop(client, None)
            else:
                self._failures[client] = recent
            return len(recent) >= MAX_FAILURES

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

            def _client(self) -> str:  # behind the ingress the peer is the proxy
                return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

            def _auth(self) -> bool:
                who, header = self._client(), self.headers.get("Authorization")
                # the robot's calls are never locked out: behind a proxy everyone shares one address,
                # and guesses at the dashboard must not silence the robot
                robot_call = self.path.split("?")[0].startswith(ROBOT_PATHS)
                if not robot_call and dash.locked_out(who):
                    self._json(429, {"error": "too many wrong passwords; wait a few minutes"})
                    return False
                if dash._authorized(header, self.path):
                    if header:
                        dash.locked_out(who, failed=False)
                    return True
                if header and not robot_call:  # a browser's first request carries none: only real attempts count
                    dash.locked_out(who, failed=True)
                    log.warning("dashboard: wrong password from %s for %s", who, self.path.split("?")[0])
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
                if self.path == "/api/intake":
                    return self._json(200, {"on": bool(dash.intake_url), "active": dash.intake_active(),
                                            "pair_on": dash.pairing_on(), "pairing": dash.pairing_active(),
                                            "pair_left": round(dash.pairing.seconds_left) if dash.pairing_active() else 0,
                                            **dash.intake_view})
                if self.path == "/api/overview":
                    return self._json(200, dash.overview())
                if self.path.startswith("/api/voice/log"):
                    return self._json(200, {"turns": dash.voice_log()})
                if self.path.startswith("/api/voice/clip/"):
                    name = os.path.basename(self.path.rsplit("/", 1)[-1])  # no path tricks
                    path = os.path.join(dash.archive_dir or "", name)
                    if not (name.endswith(".wav") and dash.archive_dir and os.path.isfile(path)):
                        return self._json(404, {"error": "gone"})
                    with open(path, "rb") as f:
                        return self._send(200, f.read(), "audio/wav")
                if self.path == "/api/episodes":
                    return self._json(200, {"episodes": dash.memory[0].list() if dash.memory else []})
                if self.path == "/api/audit":
                    return self._json(200, {"audit": dash.memory[1].recent() if dash.memory else []})
                if self.path == "/api/notes":
                    store = getattr(dash.bridge.cortex, "store", None) if dash.bridge.cortex else None
                    return self._json(200, {"notes": store.notes if store else [],
                                            "reminders": store.reminders if store else []})
                if self.path == "/api/people":
                    return self._json(200, {"people": dash.faces[1].list() if dash.faces else [],
                                            "on": bool(dash.faces), "staff": bool(dash.faces and dash.faces[2])})
                if self.path.startswith("/api/staff?"):
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("q", [""])[0]
                    if not (dash.faces and dash.faces[2]) or len(q.strip()) < 2:
                        return self._json(200, {"staff": []})
                    return self._json(200, {"staff": dash.faces[2].search(q)})
                if self.path.split("?")[0] == "/api/camera.jpg":  # one frame from the robot's camera
                    b = dash.bridge
                    if hasattr(b, "stream"):  # someone is watching: let the robot send frames itself
                        b.stream()
                    jpeg = b.snapshot(8.0) if hasattr(b, "snapshot") else None
                    if not jpeg:
                        return self._json(504, {"error": "no frame from the robot"})
                    return self._send(200, jpeg, "image/jpeg")
                if self.path.startswith("/api/filler/"):
                    wav = dash.filler(self.path.rsplit("/", 1)[-1])
                    return self._send(200, wav, "audio/wav") if wav else self._json(404, {"error": "no such filler"})
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
                # Another site's page must not post with a logged-in browser's saved password (CSRF):
                # a browser always sends Origin on such a request; the robot sends none.
                origin = self.headers.get("Origin")
                if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
                    return self._json(403, {"error": "cross-site request refused"})
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
                if dash.intake_active():  # history taking: the patient's words go to the interview server only
                    dash.intake.heard(pcm)
                    return self._send(204, b"", "audio/wav")
                if dash.collect and dash.tts:
                    return self._send(200, to_16k(dash.tts.speak(dash.keep_wake_sample(pcm))), "audio/wav")
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
                if dash.pairing_active() and text:  # the QR is up: the patient may answer without the name
                    if INTAKE_STOP_RE.search(text):
                        dash.cancel_pairing()
                        log.info("voice: %r -> pairing cancelled", text)
                        return self._send(204, b"", "audio/wav")
                    if INTAKE_SKIP_RE.search(text):
                        try:
                            dash.start_intake("th")
                            log.info("voice: %r -> history taking without a login", text)
                            return self._send(204, b"", "audio/wav")
                        except Exception as e:
                            log.warning("voice: could not start history taking: %s", e)
                if dash.intake_url and text and INTAKE_START_RE.search(text) and not INTAKE_STOP_RE.search(text):
                    try:
                        if dash.pairing_on() and not INTAKE_SKIP_RE.search(text):
                            dash.start_pairing("th")
                            log.info("voice: %r -> history taking, QR for ThaID", text)
                        else:
                            dash.start_intake("th")
                            log.info("voice: %r -> history taking", text)
                        return self._send(204, b"", "audio/wav")  # the interview's greeting follows as an announcement
                    except Exception as e:
                        log.warning("voice: could not start history taking: %s", e)
                presence = getattr(dash.bridge, "presence", None)
                if presence and text:
                    presence.note_voice()  # someone is talking next to the robot: they are here
                window = dash.follow_up_s * (3 if presence and presence.present else 1)  # at the desk: still talking
                follow_up = time.monotonic() - dash._last_reply < window
                # At the desk and talking about the robot itself ("เห็นผมไหม", "หันหน้ามา"): that is
                # addressed to it even when the ASR lost the name.
                if wake and presence and presence.present and ADDRESSED_RE.search(text):
                    follow_up = True
                # A call is the name said alone, which asr-typhoon hears as one short word ("หวี่" came
                # out as มี / นี่ / วี): answer it and open the follow-up window for the question.
                if wake and not follow_up and text and len(text.replace(" ", "")) <= CALL_MAX_CHARS:
                    dash._last_reply = time.monotonic()
                    log.info("voice: call %r (%.1fs) -> %r", text, seconds, CALL_REPLY)
                    dash.archive_clip(pcm, {"text": text, "wake": wake, "outcome": "call"})
                    return self._send(200, to_16k(dash.tts.speak(CALL_REPLY)), "audio/wav")
                if not text or (wake and not follow_up and not dash.wake.search(text)):  # not for the robot
                    log.info("voice: %s (%.1fs) -> 204", f"heard {text!r} without the wake name" if text
                             else "nothing understood", seconds)
                    dash.archive_clip(pcm, {"text": text, "wake": wake, "outcome": "no-name" if text else "empty"})
                    return self._send(204, b"", "audio/wav")
                dash.add("you", {"text": text, "voice": True})
                t_asr = time.monotonic()
                try:
                    with dash.progress() as said:
                        reply = dash.bridge.cortex.ask(text, on_lookup=lambda tool: said("search"))
                    t_llm = time.monotonic()
                    wav = to_16k(dash.tts.speak(reply)) if reply else b""
                except Exception as e:
                    log.warning("voice: %r failed: %s: %s", text, type(e).__name__, e)
                    return self._json(502, {"error": f"{type(e).__name__}: {e}"})
                log.info("voice: %r (%.1fs, wake=%d%s) -> %r | asr %.1fs llm %.1fs tts %.1fs, %d KB", text,
                         seconds, wake, ", follow-up" if follow_up else "", reply, t_asr - t0, t_llm - t_asr,
                         time.monotonic() - t_llm, len(wav) // 1024)
                dash._last_reply = time.monotonic()
                dash.archive_clip(pcm, {"text": text, "wake": wake, "outcome": "answered", "reply": reply})
                return self._send(200, wav, "audio/wav") if wav else self._send(204, b"", "audio/wav")

        return Handler
