"""Photos the user asks the robot to take ("ถ่ายรูปให้หน่อย"), kept in ``<--state-dir>/photos``.

Each photo can be opened on a phone through a short-lived share link (``/p/<token>``) that the
robot shows as a QR. The link needs no dashboard login, so it is random, lasts ``SHARE_S`` and
names one photo only; the album itself stays behind the dashboard's login.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
import time

SHARE_S = 600.0
KEEP = 300  # oldest photos go first beyond this, so the volume can't fill up
KEEP_DAYS = 30.0  # photos show people's faces (PDPA): none is kept longer than this, taken or not
NAME_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?\.jpg$")


class PhotoAlbum:
    def __init__(self, folder: str, keep: int = KEEP, keep_days: float = KEEP_DAYS):
        self.folder, self.keep, self.keep_days = folder, keep, keep_days
        self._shares: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def save(self, jpeg: bytes) -> str:
        os.makedirs(self.folder, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name, n = f"{stamp}.jpg", 1
        while os.path.exists(os.path.join(self.folder, name)):
            n += 1
            name = f"{stamp}-{n}.jpg"
        with open(os.path.join(self.folder, name), "wb") as f:
            f.write(jpeg)
        for old in self.names()[self.keep:]:
            self.delete(old)
        self.prune()
        return name

    def prune(self, now: float | None = None) -> list[str]:
        """Delete photos older than ``keep_days`` (by file time); returns their names."""
        if self.keep_days <= 0:
            return []
        cutoff = (time.time() if now is None else now) - self.keep_days * 86400
        gone = []
        for name in self.names():
            try:
                if os.path.getmtime(os.path.join(self.folder, name)) < cutoff and self.delete(name):
                    gone.append(name)
            except OSError:
                pass
        return gone

    def start_pruning(self, every_s: float = 3600.0) -> threading.Thread:
        """Prune on a timer too: with no new photo, save() never runs to clear old ones."""
        def loop():
            while True:
                self.prune()
                time.sleep(every_s)
        t = threading.Thread(target=loop, name="photo-prune", daemon=True)
        t.start()
        return t

    def names(self) -> list[str]:
        """Newest first."""
        try:
            return sorted((n for n in os.listdir(self.folder) if NAME_RE.match(n)), reverse=True)
        except FileNotFoundError:
            return []

    def path(self, name: str) -> str | None:
        if not NAME_RE.match(name or ""):
            return None
        p = os.path.join(self.folder, name)
        return p if os.path.exists(p) else None

    def read(self, name: str) -> bytes | None:
        p = self.path(name)
        if not p:
            return None
        with open(p, "rb") as f:
            return f.read()

    def delete(self, name: str) -> bool:
        p = self.path(name)
        if not p:
            return False
        os.remove(p)
        with self._lock:
            for t in [t for t, (n, _) in self._shares.items() if n == name]:
                del self._shares[t]
        return True

    def share(self, name: str, seconds: float = SHARE_S) -> str:
        if not self.path(name):
            raise ValueError("no such photo")
        token = secrets.token_urlsafe(16)
        now = time.monotonic()
        with self._lock:
            self._shares = {t: v for t, v in self._shares.items() if v[1] > now}
            self._shares[token] = (name, now + seconds)
        return token

    def shared(self, token: str) -> str | None:
        with self._lock:
            v = self._shares.get(token or "")
        return v[0] if v and v[1] > time.monotonic() and self.path(v[0]) else None
