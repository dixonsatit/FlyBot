"""Guard mode ("เฝ้าห้องให้หน่อย"): the robot rests with its screen dark and watches the room.

Motion in front of the camera takes a frame (the head stays put: the user turns it from the
dashboard while watching live). A face the
robot knows is left alone; a face it doesn't, or a person with no face in view (a vision-model
check), is kept in ``<--state-dir>/guard`` and sent to LINE with the photo, at most once per
``alert_gap_s``. Frames with nobody in them are not kept. Calling the robot by name ends it,
and that is reported too, with a photo of who did.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger("flybot.guard")

GUARD_RE_TEXT = r"เฝ้า\s*(ห้อง|บ้าน|ให้|ที่นี่)|(โหมด|เปิด)\s*กล้อง\s*วงจร\s*ปิด"
GUARD_REPLY = "ได้ครับ หนูจะเฝ้าห้องให้ ถ้าเห็นคนแปลกหน้าจะส่งรูปเข้าไลน์นะครับ เรียกน้องหวี่เมื่อไหร่ก็เลิกเฝ้าครับ"


class Guard:
    def __init__(self, snapshot: Callable[[float], bytes | None], embed: Callable[[bytes], list] | None,
                 known: Callable[[dict], dict | None], person_visible: Callable[[bytes], bool | None] | None,
                 keep: Callable[[bytes], str], alert: Callable[[bytes, str, str], None],
                 alert_gap_s: float = 300.0, check_gap_s: float = 3.0, clock: Callable[[], float] = time.monotonic):
        self.snapshot, self.embed, self.known, self.person_visible = snapshot, embed, known, person_visible
        self.keep, self.alert = keep, alert
        self.alert_gap_s, self.check_gap_s, self.clock = alert_gap_s, check_gap_s, clock
        self.on = False
        self.since = 0.0  # wall clock it was turned on
        self.seen: list[dict] = []  # this watch's sightings, newest last: {"at", "photo", "what"}
        self._last_check = self._last_alert = -float("inf")
        self._busy = threading.Lock()

    def start(self) -> None:
        if self.on:
            return
        self.on, self.since, self.seen = True, time.time(), []
        self._last_alert = -float("inf")
        log.info("guard: on")

    def stop(self) -> None:
        if not self.on:
            return
        self.on = False
        log.info("guard: off after %.0f min, %d sightings", (time.time() - self.since) / 60, len(self.seen))

    def on_motion(self) -> None:
        """The robot's camera saw motion (any thread)."""
        now = self.clock()
        if not self.on or now - self._last_check < self.check_gap_s:
            return
        self._look()

    def _look(self) -> None:
        if not self._busy.acquire(blocking=False):
            return
        self._last_check = self.clock()
        threading.Thread(target=self._check, name="guard-check", daemon=True).start()

    def _check(self) -> None:
        try:
            jpeg = self.snapshot(8.0)
            if jpeg:
                self.consider(jpeg)
        except Exception as e:  # face server or vision model down: try on the next motion
            log.warning("guard: check failed: %s", e)
        finally:
            self._busy.release()

    def consider(self, jpeg: bytes) -> str | None:
        """Decide about one frame; returns what was reported ("stranger", "person") or None."""
        faces = self.embed(jpeg) if self.embed else []
        if faces:
            people = [self.known(f) for f in faces]
            if all(people):
                log.info("guard: %s, known", ", ".join(p.get("nick") or p.get("name", "?") for p in people))
                return None
            what, text = "stranger", "มีคนแปลกหน้าอยู่ในห้อง"
        else:
            visible = self.person_visible(jpeg) if self.person_visible else None
            if visible is False:
                return None
            # None: no vision model to ask, so motion is all there is to go on
            what, text = ("person", "มีคนอยู่ในห้อง แต่ไม่เห็นหน้า") if visible else ("motion", "มีความเคลื่อนไหวในห้อง")
        name = self.keep(jpeg)
        self.seen.append({"at": time.time(), "photo": name, "what": what})
        now = self.clock()
        if now - self._last_alert >= self.alert_gap_s:
            self._last_alert = now
            self.alert(jpeg, name, f"{text} ({time.strftime('%H:%M')})")
        log.info("guard: %s, kept %s", what, name)
        return what

    def ended_by_voice(self) -> None:
        """Called by name: guard ends, and whoever did it is reported with a photo."""
        if not self.on:
            return
        self.stop()
        try:
            jpeg = self.snapshot(5.0)
        except Exception:
            jpeg = None
        if jpeg:
            self.alert(jpeg, self.keep(jpeg), f"เลิกเฝ้าห้องแล้ว มีคนเรียกน้องหวี่ ({time.strftime('%H:%M')})")
