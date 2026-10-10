"""The assistant's own memory: notes the user asked it to remember, and reminders it speaks
when they fall due. Kept as one JSON file (on a volume in k8s) so they survive restarts.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


class AssistantStore:
    def __init__(self, path: str | None, on_due: Callable[[str], None] | None = None, tz: str | None = None,
                 now: Callable[[], datetime] | None = None):
        self.path, self.on_due = path, on_due
        self.tz = ZoneInfo(tz or os.environ.get("FLYBOT_TZ") or "Asia/Bangkok")
        self.now = now or (lambda: datetime.now(self.tz))
        self.notes: list[dict] = []  # {"id", "text", "at"}
        self.reminders: list[dict] = []  # {"id", "text", "due"} (ISO, local time)
        self._next_id = 1
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._load()

    # -- persistence -------------------------------------------------------------
    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("assistant store %s unreadable: %s", self.path, e)
            return
        self.notes, self.reminders = data.get("notes", []), data.get("reminders", [])
        self._next_id = 1 + max([x["id"] for x in self.notes + self.reminders] or [0])

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"notes": self.notes, "reminders": self.reminders}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    # -- notes ----------------------------------------------------------------------
    def remember(self, text: str) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("empty note")
        with self._lock:
            note = {"id": self._id(), "text": text, "at": self.now().strftime("%Y-%m-%d %H:%M")}
            self.notes.append(note)
            self._save()
        return note

    def forget(self, note_id: int) -> bool:
        with self._lock:
            before = len(self.notes)
            self.notes = [n for n in self.notes if n["id"] != note_id]
            self._save()
            return len(self.notes) < before

    # -- reminders ------------------------------------------------------------------
    def remind(self, text: str, in_minutes: float | None = None, at: str | None = None) -> dict:
        """``at`` is local time "YYYY-MM-DD HH:MM" (or "HH:MM" for the next such time today/tomorrow)."""
        now = self.now()
        if in_minutes is not None:
            due = now + timedelta(minutes=float(in_minutes))
        elif at:
            try:
                due = datetime.strptime(at.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=self.tz)
            except ValueError:
                t = datetime.strptime(at.strip(), "%H:%M")
                due = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                if due <= now:
                    due += timedelta(days=1)
        else:
            raise ValueError("give in_minutes or at")
        if due <= now:
            raise ValueError(f"{due:%Y-%m-%d %H:%M} is already past")
        with self._lock:
            reminder = {"id": self._id(), "text": text.strip(), "due": due.isoformat(timespec="minutes")}
            self.reminders.append(reminder)
            self.reminders.sort(key=lambda r: r["due"])
            self._save()
        return {**reminder, "due": due.strftime("%Y-%m-%d %H:%M")}

    def cancel(self, reminder_id: int) -> bool:
        with self._lock:
            before = len(self.reminders)
            self.reminders = [r for r in self.reminders if r["id"] != reminder_id]
            self._save()
            return len(self.reminders) < before

    def due(self) -> list[dict]:
        """Reminders that fell due (removed once returned)."""
        now = self.now()
        with self._lock:
            fired = [r for r in self.reminders if datetime.fromisoformat(r["due"]) <= now]
            if fired:
                self.reminders = [r for r in self.reminders if r not in fired]
                self._save()
        return fired

    def summary(self) -> dict:
        with self._lock:
            return {"notes": list(self.notes),
                    "reminders": [{**r, "due": datetime.fromisoformat(r["due"]).strftime("%Y-%m-%d %H:%M")}
                                  for r in self.reminders]}

    # -- background -----------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._run, name="assistant", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            for r in self.due():
                log.info("reminder due: %s", r["text"])
                if self.on_due:
                    try:
                        self.on_due(f"เตือนครับ: {r['text']}")
                    except Exception:
                        log.exception("reminder announce failed")
            self._stop.wait(10.0)
