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
    """Notes and reminders, kept by a flybot.memory backend: the JSON file at ``path`` (the
    original format, so an existing file loads as is) or PostgreSQL when ``backend`` is given."""

    def __init__(self, path: str | None, on_due: Callable[[str], None] | None = None, tz: str | None = None,
                 now: Callable[[], datetime] | None = None, backend=None):
        from .memory import JsonBackend
        self.path, self.on_due = path, on_due
        self.tz = ZoneInfo(tz or os.environ.get("FLYBOT_TZ") or "Asia/Bangkok")
        self.now = now or (lambda: datetime.now(self.tz))
        self.backend = backend or JsonBackend(path)
        self.notes: list[dict] = self.backend.load("notes")  # {"id", "text", "at"}
        self.reminders: list[dict] = sorted(self.backend.load("reminders"), key=lambda r: r["due"])  # due: ISO local
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def import_json(self, path: str) -> int:
        """Copy notes and reminders from an old assistant.json into this (empty) store, once."""
        if self.notes or self.reminders or not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for table, rows in (("notes", data.get("notes", [])), ("reminders", data.get("reminders", []))):
            for r in rows:
                getattr(self, table).append(self.backend.insert(table, {k: v for k, v in r.items() if k != "id"}))
        self.reminders.sort(key=lambda r: r["due"])
        return len(self.notes) + len(self.reminders)

    # -- notes ----------------------------------------------------------------------
    def remember(self, text: str) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("empty note")
        with self._lock:
            note = self.backend.insert("notes", {"text": text, "at": self.now().strftime("%Y-%m-%d %H:%M")})
            self.notes.append(note)
        return note

    def forget(self, note_id: int) -> bool:
        with self._lock:
            self.notes = [n for n in self.notes if n["id"] != note_id]
            return self.backend.delete("notes", note_id)

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
            reminder = self.backend.insert("reminders", {"text": text.strip(), "due": due.isoformat(timespec="minutes")})
            self.reminders.append(reminder)
            self.reminders.sort(key=lambda r: r["due"])
        return {**reminder, "due": due.strftime("%Y-%m-%d %H:%M")}

    def cancel(self, reminder_id: int) -> bool:
        with self._lock:
            self.reminders = [r for r in self.reminders if r["id"] != reminder_id]
            return self.backend.delete("reminders", reminder_id)

    def due(self) -> list[dict]:
        """Reminders that fell due (removed once returned)."""
        now = self.now()
        with self._lock:
            fired = [r for r in self.reminders if datetime.fromisoformat(r["due"]) <= now]
            if fired:
                self.reminders = [r for r in self.reminders if r not in fired]
                for r in fired:
                    self.backend.delete("reminders", r["id"])
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
