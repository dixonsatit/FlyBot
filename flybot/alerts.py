"""LINE messages to the people who look after the robot when it needs them (``--alert-line``).

Nobody watches the dashboard: a robot that dropped off the network, restarted after a crash, ran
its battery down in an empty room or failed a firmware update went unnoticed until someone
walked past it. Each kind is sent at most once per ``cooldown_s``; going offline is one message
per outage, with another when it comes back.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger("flybot.alerts")

CRASH_RESETS = ("panic", "int-watchdog", "task-watchdog", "watchdog", "brownout")


class Alerts:
    def __init__(self, send: Callable[[str, str], object], to: list[str], offline_s: float = 300.0,
                 cooldown_s: float = 1800.0, clock: Callable[[], float] = time.time, background: bool = True):
        self.send, self.to = send, [t for t in to if t]
        self.offline_s, self.cooldown_s, self.clock = offline_s, cooldown_s, clock
        self.background = background  # LINE takes up to 10 s: never hold the MQTT thread or control loop
        self._last: dict[str, float] = {}
        self._offline_since: float | None = None
        self._lock = threading.Lock()

    def notify(self, kind: str, text: str) -> bool:
        now = self.clock()
        with self._lock:
            if now - self._last.get(kind, -float("inf")) < self.cooldown_s:
                return False
            self._last[kind] = now
        log.warning("alert (%s): %s", kind, text)
        if self.background:
            threading.Thread(target=self._deliver, args=(text,), name="alert", daemon=True).start()
        else:
            self._deliver(text)
        return True

    def _deliver(self, text: str) -> None:
        for who in self.to:
            try:
                self.send(who, "[น้องหวี่] " + text)
            except Exception as e:  # a LINE outage must not take the bridge with it
                log.warning("alert to %s not sent: %s", who, e)

    def check_online(self, robot_seen: float) -> None:
        """Call every few seconds with the wall clock of the robot's last message (0: not since start).

        Not seen since the bridge started is not an outage: a deploy would page someone each time."""
        if not robot_seen:
            return
        gone = self.clock() - robot_seen
        if gone >= self.offline_s and self._offline_since is None:
            self._offline_since = robot_seen
            self._last.pop("offline", None)  # every outage is reported, however soon after the last
            self.notify("offline", f"หุ่นหายจากระบบมา {round(gone / 60)} นาทีแล้ว "
                                   f"(เห็นครั้งสุดท้าย {time.strftime('%H:%M', time.localtime(robot_seen))})")
        elif gone < self.offline_s and self._offline_since is not None:
            away = robot_seen - self._offline_since
            self._offline_since = None
            self._last.pop("online", None)
            self.notify("online", f"หุ่นกลับมาออนไลน์แล้ว หายไป {round(away / 60)} นาที")

    def selftest(self, report: dict) -> None:
        if report.get("reset") in CRASH_RESETS:
            last = report.get("last") or "ไม่ทราบ"
            self.notify("crash", f"หุ่นรีสตาร์ตเพราะ {report['reset']} ตอนกำลัง {last}"
                                 + (f" loop ค้างที่ {report['loop']}" if report.get("loop") else "")
                                 + (f" (firmware {report['fw']})" if report.get("fw") else ""))

    def battery(self, level: int, someone_there: bool) -> None:
        if not someone_there:  # with someone there the robot says it out loud instead
            self.notify("battery", f"แบตหุ่นเหลือ {level}% และไม่มีใครอยู่ใกล้ ช่วยเสียบชาร์จด้วย")

    def ota(self, status: dict) -> None:
        if "error" in status:
            self._last.pop("ota", None)  # each failed update is its own news
            self.notify("ota", f"อัปเดต firmware ไม่สำเร็จ: {status['error']}")
