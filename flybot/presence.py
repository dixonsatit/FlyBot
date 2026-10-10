"""Is the user at the desk? Motion alone can't tell a person from a curtain, so motion (or a
voice turn) triggers a camera frame that a small vision model answers yes/no on. The assistant
holds announcements while nobody is there and, on the user's return, says what it held and
asks about a meeting they were away for.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

PROMPT = ("Is any part of a person visible in this camera frame (face, head, shoulders, arms, body)? "
          "If yes, reply 'yes X Y' with X and Y the centre of their face as fractions of the image "
          "width and height (0 = left/top, 1 = right/bottom); if the face is out of frame, give the "
          "centre of the visible part instead. If no person, reply 'no'. Reply with only that line.")
_POSITION = re.compile(r"\byes\b\D{0,20}?(\d(?:\.\d+)?)\D{1,10}?(\d(?:\.\d+)?)", re.IGNORECASE)
_YES = re.compile(r"\byes\b|ใช่", re.IGNORECASE)
_NO = re.compile(r"\bno\b|ไม่มี", re.IGNORECASE)


class Presence:
    def __init__(self, snapshot: Callable[[float], bytes | None], vision_llm,
                 on_arrive: Callable[[float, float], None] | None = None, check_s: float = 30.0,
                 away_s: float = 300.0, clock: Callable[[], float] = time.time,
                 on_person: Callable[[float, float], None] | None = None, track_s: float = 8.0):
        self.snapshot, self.vision = snapshot, vision_llm
        self.on_arrive = on_arrive  # (left_at, now) when the user comes back
        self.check_s, self.away_s, self.clock = check_s, away_s, clock
        self.on_person, self.track_s = on_person, track_s  # where the face is (0..1), re-checked while present
        self.present = False
        self.last_seen = -float("inf")  # wall clock, so meeting times compare
        self.left_at: float | None = None
        self._motion = -float("inf")
        self._last_check = -float("inf")
        self._stop = threading.Event()

    # -- inputs (any thread) ----------------------------------------------------------
    def note_motion(self) -> None:
        self._motion = self.clock()

    def note_voice(self) -> None:
        """Someone spoke to the robot: they are here."""
        self._seen()

    # -- state ------------------------------------------------------------------------
    def _seen(self) -> None:
        now = self.clock()
        self.last_seen = now
        if not self.present:
            self.present = True
            left = self.left_at
            log.info("presence: user here%s", f" (away {(now - left) / 60:.0f} min)" if left else "")
            if self.on_arrive and left is not None:
                try:
                    self.on_arrive(left, now)
                except Exception:
                    log.exception("on_arrive failed")
            self.left_at = None

    def person_visible(self) -> bool | None:
        jpeg = self.snapshot(3.0)
        if not jpeg:
            return None
        reply = self.vision.ask("You check a desk robot's camera frame.", [], PROMPT, image_jpeg=jpeg)
        return self.parse(reply)

    def parse(self, reply: str) -> bool:
        """'yes X Y' anywhere in the reply (Haiku sometimes explains first) -> seen, at (X, Y)."""
        m = _POSITION.search(reply)
        if m:
            x, y = (min(1.0, max(0.0, float(v))) for v in m.groups())
            if self.on_person:
                self.on_person(x, y)
            return True
        yes, no = list(_YES.finditer(reply)), list(_NO.finditer(reply))
        return bool(yes) and (not no or yes[-1].start() > no[-1].start())  # the last verdict wins

    def tick(self) -> None:
        now = self.clock()
        if self.present and now - self.last_seen > self.away_s:
            self.present, self.left_at = False, self.last_seen
            log.info("presence: user away since %s", time.strftime("%H:%M", time.localtime(self.last_seen)))
        recent_motion = now - self._motion < 15.0
        every = self.track_s if self.present else self.check_s
        if (recent_motion or self.present) and now - self._last_check >= every:
            self._last_check = now
            try:
                seen = self.person_visible()
            except Exception as e:  # vision model down: decide on the next round
                log.warning("presence check failed: %s", e)
                return
            if seen:
                self._seen()

    def start(self) -> None:
        threading.Thread(target=self._run, name="presence", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(5.0)
