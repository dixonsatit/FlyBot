"""Who is in front of the robot: face embeddings from tools/face_server.py (DGX), matched against
people who agreed to be enrolled. Only unit-length SFace features and the consent time are kept,
never pictures. Staff details (name, nickname, position, unit) come from a narrow read-only view
when one is configured (StaffDirectory), so the bridge never holds the full HR record.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

# OpenCV's SFace: cosine >= 0.363 is the same person (its paper's threshold on LFW).
MATCH_COSINE = 0.363


class FaceClient:
    def __init__(self, url: str, token: str = "", timeout: float = 10.0):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def embed(self, jpeg: bytes) -> list[dict]:
        req = urllib.request.Request(f"{self.url}/embed", data=jpeg, method="POST",
                                     headers={"Content-Type": "image/jpeg", "X-Token": self.token})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)["faces"]


class StaffDirectory:
    """Look staff up in a view that holds only what the robot may say: code, title, name,
    surname, nicname, position, office_name (e.g. hospdata.v_stackchan_staff)."""
    COLUMNS = "code, title, name, surname, nicname, position, office_name"

    def __init__(self, dsn: str, view: str = "v_stackchan_staff"):
        u = urllib.parse.urlparse(dsn)
        self.conn_args = dict(host=u.hostname, port=u.port or 3306, user=urllib.parse.unquote(u.username or ""),
                              password=urllib.parse.unquote(u.password or ""), database=u.path.lstrip("/"),
                              charset="utf8mb4", connect_timeout=8)
        self.view = view

    def _query(self, where: str, args: tuple) -> list[dict]:
        import pymysql
        with pymysql.connect(**self.conn_args, cursorclass=pymysql.cursors.DictCursor) as c, c.cursor() as cur:
            cur.execute(f"SELECT {self.COLUMNS} FROM {self.view} WHERE {where} LIMIT 10", args)
            return list(cur.fetchall())

    def get(self, code: int) -> dict | None:
        rows = self._query("code = %s", (code,))
        return rows[0] if rows else None

    def search(self, text: str) -> list[dict]:
        like = f"%{text.strip()}%"
        return self._query("name LIKE %s OR surname LIKE %s OR nicname LIKE %s", (like, like, like))


class FaceRegistry:
    """Enrolled people in a JSON file: {"people": {id: {name, nick, position, office, code,
    consent_at, embeddings: [[128 floats], ...]}}}."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.people: dict[str, dict] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.people = json.load(f).get("people", {})

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"people": self.people}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def enroll(self, pid: str, info: dict, embeddings: list[list[float]]) -> dict:
        if not embeddings:
            raise ValueError("no face in the pictures")
        with self.lock:
            p = self.people.setdefault(pid, {"embeddings": []})
            p.update({k: v for k, v in info.items() if k != "embeddings" and v})  # adding pictures keeps the details
            p["embeddings"] = (p["embeddings"] + [list(map(float, e)) for e in embeddings])[-20:]
            p.setdefault("consent_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            self._save()
            return self.summary(pid)

    def forget(self, pid: str) -> bool:
        with self.lock:
            gone = self.people.pop(pid, None) is not None
            if gone:
                self._save()
            return gone

    EDITABLE = ("name", "nick", "code", "position", "office")

    def update(self, pid: str, fields: dict) -> dict:
        """Change who someone is (not their face): name, nickname, employee code, position, office."""
        with self.lock:
            if pid not in self.people:
                raise ValueError("no such person")
            p = self.people[pid]
            for k in self.EDITABLE:
                if k not in fields:
                    continue
                v = fields[k]
                if k == "code":
                    v = str(v).strip()
                    if v and not v.isdigit():
                        raise ValueError("employee code must be a number")
                    v = int(v) if v else None
                else:
                    v = str(v or "").strip()[:80]
                if k == "name" and not v:
                    raise ValueError("name can't be empty")
                p[k] = v
            self._save()
        return self.summary(pid)

    def summary(self, pid: str) -> dict:
        p = self.people[pid]
        return {"id": pid, **{k: v for k, v in p.items() if k != "embeddings"}, "samples": len(p["embeddings"])}

    def list(self) -> list[dict]:
        with self.lock:
            return [self.summary(pid) for pid in self.people]

    def match(self, embedding: list[float]) -> tuple[str | None, float]:
        """Best enrolled person for one face, or (None, best score) below MATCH_COSINE."""
        e = np.asarray(embedding, dtype=np.float32)
        best, best_score = None, -1.0
        with self.lock:
            for pid, p in self.people.items():
                if p["embeddings"]:
                    score = float(np.max(np.asarray(p["embeddings"], dtype=np.float32) @ e))
                    if score > best_score:
                        best, best_score = pid, score
        return (best, best_score) if best_score >= MATCH_COSINE else (None, best_score)


class Greeter:
    """While the robot sees a face, look who it is every ``every_s`` and greet each known
    person once per ``greet_gap_s``. Someone it doesn't know gets ``on_stranger`` (a hello and
    the robot's name), but nothing of their face is kept, so it can't tell strangers apart:
    one stranger greeting per ``stranger_gap_s`` for everyone."""

    # A stranger is greeted only when clearly nobody known (a blurred frame of a known face scores
    # just under MATCH_COSINE), close enough to be talking to the robot, and still there a few seconds later.
    STRANGER_BELOW = MATCH_COSINE - 0.08
    STRANGER_MIN_WIDTH = 0.15  # of the frame
    STRANGER_STAY_S = 3.0

    def __init__(self, snapshot: Callable[[float], bytes | None], client: FaceClient, registry: FaceRegistry,
                 on_greet: Callable[[dict], None], every_s: float = 4.0, greet_gap_s: float = 6 * 3600,
                 clock: Callable[[], float] = time.time, on_stranger: Callable[[], None] | None = None,
                 stranger_gap_s: float = 300.0):
        self.snapshot, self.client, self.registry, self.on_greet = snapshot, client, registry, on_greet
        self.on_stranger, self.stranger_gap_s = on_stranger, stranger_gap_s
        self._stranger_since: float | None = None  # first look of the stranger now in front of it
        self._stranger_greeted = -float("inf")
        self.every_s, self.greet_gap_s, self.clock = every_s, greet_gap_s, clock
        self.greeted: dict[str, float] = {}
        self.present: dict | None = None  # who was recognised last, for the assistant's context
        self.present_at = 0.0
        self._last = -float("inf")
        self._busy = threading.Lock()

    def on_face_seen(self) -> None:
        """The robot's detector found a face (any thread): check who it is, at most every every_s."""
        now = self.clock()
        if now - self._last < self.every_s or not (self.registry.people or self.on_stranger) \
                or not self._busy.acquire(blocking=False):
            return
        self._last = now
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self) -> None:
        try:
            jpeg = self.snapshot(8.0, 1.0)  # a frame up to 1 s old will do
            if not jpeg:
                return
            self.consider(self.client.embed(jpeg))
        except Exception as e:
            log.warning("face check failed: %s", e)
        finally:
            self._busy.release()

    def consider(self, faces: list[dict]) -> None:
        """Faces found in a robot frame (largest first): remember and greet a known one."""
        if not faces:
            self._stranger_since = None
            return
        pid, score = self.registry.match(faces[0]["embedding"]) if self.registry.people else (None, -1.0)
        if not pid:
            self._consider_stranger(faces[0], score)
            return
        self._stranger_since = None
        person = self.registry.summary(pid)
        if not self.present or self.present.get("id") != pid or self.clock() - self.present_at > 60:
            log.info("faces (robot camera): %s (cosine %.3f)", person.get("name"), score)
        self.present, self.present_at = person, self.clock()
        if self.clock() - self.greeted.get(pid, -float("inf")) >= self.greet_gap_s:
            self.greeted[pid] = self.clock()
            self.on_greet(person)

    def _consider_stranger(self, face: dict, score: float) -> None:
        box = face.get("box") or [0, 0, 0, 0]
        if score >= self.STRANGER_BELOW or box[2] < self.STRANGER_MIN_WIDTH or not self.on_stranger:
            self._stranger_since = None
            return
        now = self.clock()
        if self._stranger_since is None:
            self._stranger_since = now
        # the tracker looks several times a second: "still there" means for STRANGER_STAY_S
        if now - self._stranger_since < self.STRANGER_STAY_S or now - self._stranger_greeted < self.stranger_gap_s \
                or (self.present and now - self.present_at < 60):  # with someone it knows: they're together
            return
        self._stranger_greeted = now
        log.info("faces (robot camera): someone new (best cosine %.3f), greeting", score)
        self.on_stranger()

    def who(self, within_s: float = 120.0) -> dict | None:
        return self.present if self.present and self.clock() - self.present_at <= within_s else None


class FaceTracker:
    """Keep the face in front of the robot centred, from the DGX's detections: while someone is
    around, the robot streams frames (~3/s), each goes to the face server, and the head turns a
    part of the way (gain) towards the largest face. The same detections feed the greeter. The
    robot's own on-device detector rarely found a face, so it is not relied on.
    """

    def __init__(self, snapshot, stream: Callable[..., None], client: FaceClient, controller, greeter: Greeter,
                 active: Callable[[], bool], gain: float = 0.8, deadband: tuple[float, float] = (0.06, 0.08),
                 fps: int = 3, idle_s: float = 20.0, clock: Callable[[], float] = time.monotonic,
                 on_seen: Callable[[], None] | None = None, follow: bool = True, turn_every_s: float = 0.0):
        self.snapshot, self.stream, self.client, self.c, self.greeter = snapshot, stream, client, controller, greeter
        # False: look (who is there, greetings) but leave the head alone; center() still turns it
        # before a photo. Following all the time kept the head moving (2026-10-17).
        self.follow = follow
        self.turn_every_s = turn_every_s  # at most one turn this often (0: every frame)
        self._last_turn = -float("inf")
        self.active, self.gain, self.deadband, self.fps, self.idle_s, self.clock = active, gain, deadband, fps, idle_s, clock
        self.on_seen = on_seen  # e.g. presence.note_face: a face means someone is there
        self.paused_until = 0.0  # a person or the assistant moved the head on purpose
        self.last_face = -float("inf")
        self._homed = True  # the head already went back to the home pose after the last face
        self.last_seen: dict | None = None  # the box of the face last centred on, for the dashboard
        # someone is here but no face in view (at tilt 10 the camera saw a seated person's chin and
        # chest): look up step by step, once, then wait before trying again
        self._search_tilts: list[float] = []
        self._next_search = 0.0
        self._stop = threading.Event()

    def pause(self, seconds: float = 15.0) -> None:
        self.paused_until = self.clock() + seconds

    def step(self) -> bool:
        """One frame: find faces, turn towards the largest. True if a face was seen."""
        now = self.clock()
        if now < self.paused_until or not self.active():
            return False
        recent = now - self.last_face < self.idle_s
        homing = self.follow and not recent and not self._homed and now - self.last_face > 30.0
        if homing:
            self._homed = True  # nobody for a while: face the desk again, once, then stay still
            self.c.face(self.c.cfg.home_pan, self.c.cfg.home_tilt, hold_s=2.0)
        if recent and self.follow:  # someone is in view: keep frames coming without a request each time
            self.stream(seconds=10.0, fps=self.fps)
        jpeg = self.snapshot(3.0, 0.0)
        if not jpeg:
            return False
        pan, tilt = self.c.pan, self.c.tilt  # the frame is a few hundred ms old; the gain absorbs it
        faces = self.client.embed(jpeg)
        if not faces:
            if self.follow and not homing:  # going back to the desk this step: no search on top of it
                self._search_up(now)
            return False
        self.last_face = self.clock()
        self._homed = False
        self._search_tilts = []
        x, y, w, h = faces[0]["box"]
        dx, dy = x + w / 2 - 0.5, y + h / 2 - 0.5
        self.last_seen = {"x": round(x + w / 2, 3), "y": round(y + h / 2, 3), "w": round(w, 3)}
        if self.follow and (abs(dx) > self.deadband[0] or abs(dy) > self.deadband[1]) \
                and now - self._last_turn >= self.turn_every_s:
            self._last_turn = now
            self.c.follow(pan + self.gain * self.c.cfg.camera_x_sign * dx * self.c.cfg.hfov_deg, tilt - self.gain * dy * self.c.cfg.vfov_deg)
        self.greeter.consider(faces)
        if self.on_seen:
            self.on_seen()
        return True

    SEARCH_STEP_DEG, SEARCH_AFTER_S, SEARCH_AGAIN_S = 12.0, 3.0, 30.0

    def _search_up(self, now: float) -> None:
        if now - self.last_face < self.SEARCH_AFTER_S:
            return
        if not self._search_tilts:
            if now < self._next_search:
                return
            lo, hi = self.c.cfg.tilt_limits
            start = self.c.tilt
            self._search_tilts = [t for t in (start + self.SEARCH_STEP_DEG * k for k in (1, 2, 3)) if t <= min(hi, 60.0)]
            self._search_tilts.append(self.c.cfg.home_tilt)  # nobody found up there: back to the desk
            self._next_search = now + self.SEARCH_AGAIN_S
        self.c.face(self.c.pan, self._search_tilts.pop(0), hold_s=1.5)

    def center(self, timeout_s: float = 5.0, tol: tuple[float, float] = (0.05, 0.07),
               hold_s: float = 15.0) -> dict | None:
        """Bring the largest face to the middle now (before a photo): look, turn, look again until it
        is centred. The tracking loop is paused meanwhile so the two don't fight. Returns where the
        face ended up ({"x", "y", "w", "centred"}), or None when nobody is in view."""
        end = time.monotonic() + timeout_s
        self.pause(timeout_s + hold_s)
        seen = None
        while time.monotonic() < end:
            jpeg = self.snapshot(3.0, 0.0)
            if not jpeg:
                break
            pan, tilt = self.c.pan, self.c.tilt
            faces = self.client.embed(jpeg)
            if not faces:
                if seen is None and self.c.tilt + self.SEARCH_STEP_DEG <= self.c.cfg.tilt_limits[1]:
                    # nobody in view yet: the face may be above the frame, look up a step
                    self.c.face(self.c.pan, self.c.tilt + self.SEARCH_STEP_DEG, hold_s=hold_s)
                    time.sleep(0.9)
                    continue
                return seen
            x, y, w, h = faces[0]["box"]
            dx, dy = x + w / 2 - 0.5, y + h / 2 - 0.5
            seen = {"x": round(x + w / 2, 3), "y": round(y + h / 2, 3), "w": round(w, 3),
                    "centred": abs(dx) <= tol[0] and abs(dy) <= tol[1]}
            self.last_seen, self.last_face = seen, self.clock()
            if seen["centred"]:
                return seen
            self.c.face(pan + 0.9 * self.c.cfg.camera_x_sign * dx * self.c.cfg.hfov_deg, tilt - 0.9 * dy * self.c.cfg.vfov_deg, hold_s=hold_s)
            time.sleep(0.9)  # let the head get there before the next look
        return seen

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                seen = self.step()
            except Exception as e:
                log.warning("face tracking: %s", e)
                seen = False
            # following a face: next frame at once; nobody in view: look again every 3 s
            if not self.follow:  # only looking: who is there changes slowly
                self._stop.wait(2.0)
                continue
            self._stop.wait(0.05 if seen or self.clock() - self.last_face < self.idle_s else 3.0)

    def start(self) -> None:
        threading.Thread(target=self.run, name="face-tracker", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
