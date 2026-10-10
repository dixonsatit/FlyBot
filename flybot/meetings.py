"""Meeting reminders from iCalendar (ICS) feeds.

Works with any calendar that publishes ICS: Google Calendar ("Secret address in iCal
format"), Outlook / Microsoft 365 (published calendar), iCloud, or a local .ics file.
Recurring events (RRULE, exceptions) are expanded with ``recurring_ical_events``;
floating times are read in ``tz``; all-day and cancelled events are skipped.

A background thread re-reads the feeds every ``refresh_s`` and, ``leads_min`` minutes
before each start, calls ``on_reminder`` once per (event, lead) with a dict:
``{"uid", "title", "start", "minutes", "location"}``.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Meeting:
    uid: str
    title: str
    start: datetime
    location: str = ""


def _read(source: str, timeout: float = 15.0) -> bytes:
    if source.startswith("webcal://"):
        source = "https://" + source[len("webcal://"):]
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"User-Agent": "flybot-meetings"})
        for attempt in range(3):  # k9s egress to Google stalls now and then; a fresh connection is quick
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
            except (OSError, TimeoutError) as e:
                if attempt == 2:
                    raise
                log.info("calendar feed attempt %d failed (%s), retrying", attempt + 1, e)
    return Path(source).expanduser().read_bytes()


def parse_meetings(ics: bytes, start: datetime, end: datetime, tz: tzinfo) -> list[Meeting]:
    """Timed, non-cancelled occurrences starting in [start, end)."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics)
    meetings = []
    for ev in recurring_ical_events.of(cal, skip_bad_series=True).between(start, end):
        dtstart = ev.get("DTSTART").dt
        if not isinstance(dtstart, datetime) or str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue  # all-day events have no time to remind at
        if dtstart.tzinfo is None:
            dtstart = dtstart.replace(tzinfo=tz)  # floating time = the robot's local time
        if not start <= dtstart < end:
            continue
        meetings.append(Meeting(uid=str(ev.get("UID", "")), title=str(ev.get("SUMMARY", "")).strip(),
                                start=dtstart.astimezone(tz), location=str(ev.get("LOCATION", "")).strip()))
    return sorted(meetings, key=lambda m: m.start)


class MeetingReminder:
    def __init__(self, sources: list[str], on_reminder: Callable[[dict], None],
                 leads_min: tuple[float, ...] = (10, 1), refresh_s: float = 300.0,
                 tz: tzinfo | None = None, grace_s: float = 120.0, horizon_days: float = 7.0,
                 now: Callable[[], datetime] | None = None):
        self.sources = sources
        self.on_reminder = on_reminder
        self.leads = sorted(leads_min, reverse=True)
        self.refresh_s = refresh_s
        self.tz = tz or datetime.now().astimezone().tzinfo
        self.horizon = timedelta(days=horizon_days)  # how far ahead the assistant can answer about
        self.grace = timedelta(seconds=grace_s)  # a reminder later than this is stale: skip it
        self.now = now or (lambda: datetime.now(self.tz))
        self.meetings: list[Meeting] = []
        self._fired: set[tuple[str, datetime, float]] = set()
        self._stop = threading.Event()

    def refresh(self) -> bool:
        now = self.now()
        found = []
        for source in self.sources:
            try:
                found += parse_meetings(_read(source), now - timedelta(hours=6), now + self.horizon, self.tz)
            except Exception as e:  # keep the last good list if a feed is down or malformed
                log.warning("calendar %s: %s", source if not source.startswith("http") else "feed", e)
                return False
        self.meetings = sorted(found, key=lambda m: m.start)
        log.info("calendar: %d meetings in the next %d days (and past 6 h)", len(self.meetings), self.horizon.days)
        return True

    def check(self) -> list[dict]:
        """Reminders due now (each fired once)."""
        now, due = self.now(), []
        for m in self.meetings:
            for lead in self.leads:
                at = m.start - timedelta(minutes=lead)
                key = (m.uid, m.start, lead)
                if at <= now < at + self.grace and key not in self._fired:
                    self._fired.add(key)
                    due.append({"uid": m.uid, "title": m.title, "start": m.start.strftime("%H:%M"),
                                "minutes": max(0, round((m.start - now).total_seconds() / 60)),
                                "location": m.location, "lead": lead})
        self._fired = {k for k in self._fired if k[1] > now - timedelta(days=1)}
        return due

    def start(self) -> None:
        threading.Thread(target=self._run, name="meetings", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        last_refresh, wait = -float("inf"), self.refresh_s
        while not self._stop.is_set():
            if time.monotonic() - last_refresh >= wait:
                ok = self.refresh()
                last_refresh = time.monotonic()
                wait = self.refresh_s if ok else min(30.0, self.refresh_s)  # failed: try again soon
            for reminder in self.check():
                try:
                    self.on_reminder(reminder)
                except Exception:
                    log.exception("meeting reminder handler failed")
            self._stop.wait(10.0)
