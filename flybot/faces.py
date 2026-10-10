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
            p.update({k: v for k, v in info.items() if k != "embeddings"})
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
    person once per ``greet_gap_s``. Unknown faces are ignored (nothing is kept)."""

    def __init__(self, snapshot: Callable[[float], bytes | None], client: FaceClient, registry: FaceRegistry,
                 on_greet: Callable[[dict], None], every_s: float = 4.0, greet_gap_s: float = 6 * 3600,
                 clock: Callable[[], float] = time.time):
        self.snapshot, self.client, self.registry, self.on_greet = snapshot, client, registry, on_greet
        self.every_s, self.greet_gap_s, self.clock = every_s, greet_gap_s, clock
        self.greeted: dict[str, float] = {}
        self.present: dict | None = None  # who was recognised last, for the assistant's context
        self.present_at = 0.0
        self._last = -float("inf")
        self._busy = threading.Lock()

    def on_face_seen(self) -> None:
        """The robot's detector found a face (any thread): check who it is, at most every every_s."""
        now = self.clock()
        if now - self._last < self.every_s or not self.registry.people or not self._busy.acquire(blocking=False):
            return
        self._last = now
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self) -> None:
        try:
            jpeg = self.snapshot(8.0)
            if not jpeg:
                return
            faces = self.client.embed(jpeg)
            if not faces:
                return
            pid, score = self.registry.match(faces[0]["embedding"])
            log.info("faces: %d in frame, nearest %s (cosine %.2f)", len(faces), pid or "unknown", score)
            if not pid:
                return
            person = self.registry.summary(pid)
            self.present, self.present_at = person, self.clock()
            if self.clock() - self.greeted.get(pid, -float("inf")) >= self.greet_gap_s:
                self.greeted[pid] = self.clock()
                self.on_greet(person)
        except Exception as e:
            log.warning("face check failed: %s", e)
        finally:
            self._busy.release()

    def who(self, within_s: float = 120.0) -> dict | None:
        return self.present if self.present and self.clock() - self.present_at <= within_s else None
