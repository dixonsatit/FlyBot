"""LINE messages to staff through the hospital's LINE OA (Messaging API push).

Only staff registered here by their employee code (``hospdata.employee.code``) can be messaged:
the bridge looks the code up in a narrow view that holds the name and the LINE user id the person
linked to the KKH LINE OA (``hospdata.covid_line_register`` by citizen id), keeps those few fields
in ``<--state-dir>/line_contacts.json``, and the assistant's ``send_line`` tool can only pick from
that list. A LINE user id belongs to one channel, so the token must be that LINE OA's.

    CREATE VIEW hospdata.v_stackchan_line AS
    SELECT e.code, e.title, e.name, e.surname, e.nicname, l.line_id, l.date
    FROM hospdata.employee e
    JOIN hospdata.covid_line_register l ON l.cid = e.person_id AND l.expire IS NULL AND l.line_id <> ''
    WHERE e.expire IS NULL;

A person can have several rows (registered again later, through another channel); registration
keeps the newest one this OA can reach: on 2026-10-16 the newest of the user's two ids was 404 to
the KKH OA and the older one worked.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

PUSH_URL = "https://api.line.me/v2/bot/message/push"
MAX_TEXT = 5000  # LINE's limit per text message


class LineError(ValueError):  # the page shows it as a 400
    pass


class LineMessenger:
    def __init__(self, token: str, push_url: str = PUSH_URL, timeout: float = 10.0):
        self.token, self.push_url, self.timeout = token, push_url, timeout

    def reachable(self, line_id: str) -> bool:
        """The person is a friend of this OA (the profile is readable)."""
        url = self.push_url.rsplit("/message/push", 1)[0] + f"/profile/{urllib.parse.quote(line_id)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout):
                return True
        except urllib.error.HTTPError as e:
            if e.code in (401, 429):
                raise LineError(f"LINE {e.code} while checking the contact") from None
            return False

    def push(self, line_id: str, text: str) -> None:
        body = json.dumps({"to": line_id, "messages": [{"type": "text", "text": text[:MAX_TEXT]}]}).encode()
        req = urllib.request.Request(self.push_url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout):
                return
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode(errors="replace")
            raise LineError({401: "LINE rejected the token (401): check the channel access token",
                             403: "LINE refused (403): they may have blocked the LINE OA or never added it",
                             429: "LINE rate limit (429): try again later"}.get(e.code, f"LINE {e.code}")
                            + f" {detail}") from None


class LineDirectory:
    """Employee code -> name and LINE user id, from the narrow view in the module docstring."""
    COLUMNS = "code, title, name, surname, nicname, line_id"

    def rows(self, code: int) -> list[dict]:
        """Every LINE registration of the employee, newest first."""
        import pymysql
        with pymysql.connect(**self.conn_args, cursorclass=pymysql.cursors.DictCursor) as c, c.cursor() as cur:
            cur.execute(f"SELECT {self.COLUMNS} FROM {self.view} WHERE code = %s ORDER BY date DESC LIMIT 10",
                        (int(code),))
            return list(cur.fetchall())

    def __init__(self, dsn: str, view: str = "v_stackchan_line"):
        u = urllib.parse.urlparse(dsn)
        self.conn_args = dict(host=u.hostname, port=u.port or 3306, user=urllib.parse.unquote(u.username or ""),
                              password=urllib.parse.unquote(u.password or ""), database=u.path.lstrip("/"),
                              charset="utf8mb4", connect_timeout=8)
        self.view = view

    def get(self, code: int) -> dict | None:
        rows = self.rows(code)
        return rows[0] if rows else None


class LineContacts:
    """Who the robot may message: registered by employee code, kept in a JSON file."""

    def __init__(self, path: str, directory, messenger: LineMessenger | None):
        self.path, self.directory, self.messenger = path, directory, messenger
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

    def register(self, code) -> dict:
        try:
            code = int(str(code).strip())
        except ValueError:
            raise ValueError("employee code must be a number") from None
        if not self.directory:
            raise ValueError("no staff directory: set --line-dsn (the v_stackchan_line view)")
        rows = [r for r in (self.directory.rows(code) if hasattr(self.directory, "rows")
                            else [self.directory.get(code)]) if r]
        if not rows:
            raise ValueError(f"no active employee with code {code}")
        ids = [r for r in rows if r.get("line_id")]
        if self.messenger and len(ids) > 1:
            ids = [r for r in ids if self.messenger.reachable(r["line_id"])] or ids[:0]
        if not ids:
            raise ValueError(f"employee {code} has not linked LINE to this LINE OA")
        row = ids[0]
        person = {"code": code, "name": f"{row.get('title') or ''}{row['name']} {row.get('surname') or ''}".strip(),
                  "nick": row.get("nicname") or "", "line_id": row["line_id"],
                  "registered_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        with self.lock:
            self.people[str(code)] = person
            self._save()
        return self.public(person)

    def rename(self, code, name: str | None = None, nick: str | None = None) -> bool:
        """Keep a contact's name and nickname in step with the people page."""
        with self.lock:
            p = self.people.get(str(code))
            if not p:
                return False
            if name:
                p["name"] = name
            if nick is not None:
                p["nick"] = nick
            self._save()
        return True

    def remove(self, code) -> bool:
        with self.lock:
            gone = self.people.pop(str(code).strip(), None) is not None
            if gone:
                self._save()
        return gone

    @staticmethod
    def public(p: dict) -> dict:
        """What the page and the LLM see: never the LINE user id."""
        return {"code": p["code"], "name": p["name"], "nick": p.get("nick", "")}

    def list(self) -> list[dict]:
        return [self.public(p) for p in self.people.values()]

    def find(self, who: str) -> dict | None:
        """A registered contact by code, nickname or name (or part of it)."""
        w = str(who).strip().removeprefix("คุณ").removeprefix("พี่").removeprefix("น้อง").strip()
        if not w:
            return None
        if w in self.people:
            return self.people[w]
        hits = [p for p in self.people.values() if w == p.get("nick") or w in p["name"]]
        return hits[0] if len(hits) == 1 else None

    def send(self, who: str, text: str) -> dict:
        if not self.messenger:
            raise ValueError("LINE is off: set --line-token (the KKH LINE OA channel access token)")
        text = str(text).strip()
        if not text:
            raise ValueError("empty message")
        p = self.find(who)
        if not p:
            names = ", ".join(f"{x['nick'] or x['name']} ({x['code']})" for x in self.people.values()) or "nobody yet"
            raise ValueError(f"'{who}' is not a registered LINE contact; registered: {names}")
        self.messenger.push(p["line_id"], text)
        log.info("line: sent %d chars to employee %s", len(text), p["code"])
        return {"ok": True, "to": self.public(p)}
