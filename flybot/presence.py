"""Is the user at the desk? Motion alone can't tell a person from a curtain, so motion (or a
voice turn) triggers a camera frame that a small vision model answers yes/no on. The assistant
holds announcements while nobody is there and, on the user's return, says what it held and
asks about a meeting they were away for.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

PROMPT = ("Is at least one person (a face, head or upper body) visible in this camera frame? "
          "Answer with exactly one word: yes or no.")


class Presence:
    def __init__(self, snapshot: Callable[[float], bytes | None], vision_llm,
                 on_arrive: Callable[[float, float], None] | None = None, check_s: float = 30.0,
                 away_s: float = 300.0, clock: Callable[[], float] = time.time):
        self.snapshot, self.vision = snapshot, vision_llm
        self.on_arrive = on_arrive  # (left_at, now) when the user comes back
        self.check_s, self.away_s, self.clock = check_s, away_s, clock
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
        return reply.strip().lower().startswith(("yes", "ใช่"))

    def tick(self) -> None:
        now = self.clock()
        if self.present and now - self.last_seen > self.away_s:
            self.present, self.left_at = False, self.last_seen
            log.info("presence: user away since %s", time.strftime("%H:%M", time.localtime(self.last_seen)))
        recent_motion = now - self._motion < 15.0
        if (recent_motion or self.present) and now - self._last_check >= self.check_s:
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
