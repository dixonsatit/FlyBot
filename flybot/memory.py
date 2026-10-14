"""Long-term memory for the assistant, with every part swappable by configuration.

Three layers:
- notes and reminders (AssistantStore): what the user asked it to keep; the latest notes ride
  in every prompt, so recalling them needs no tool call;
- episodes: short summaries of past conversations (and later of tasks), each with an embedding,
  found again with the ``recall`` tool;
- tasks and an append-only audit log, for the agent that acts (phase 2).

Where it lives is a backend chosen by ``--memory-dsn``: none = a JSON file next to the notes (as
before), ``postgresql://...`` = any PostgreSQL. Embeddings come from any OpenAI-compatible
``/v1/embeddings`` server (``--embed-url``, e.g. BGE-M3 on the DGX); without one, recall falls
back to keyword overlap. Similarity is computed here, so no database extension (pgvector) is
needed and the backend stays plain SQL.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import urllib.request
from datetime import datetime
from typing import Callable

log = logging.getLogger(__name__)


# -- embeddings ---------------------------------------------------------------------------
class Embedder:
    """An OpenAI-compatible /v1/embeddings endpoint (TEI, vLLM, Ollama, OpenAI, tools/embed_server.py)."""

    def __init__(self, url: str, model: str = "BAAI/bge-m3", api_key: str = "", timeout: float = 20.0):
        self.url, self.model, self.key, self.timeout = url.rstrip("/"), model, api_key, timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        url = self.url if self.url.endswith("/embeddings") else self.url + "/v1/embeddings"
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=self.timeout) as r:
            data = sorted(json.load(r)["data"], key=lambda d: d["index"])
        return [unit(d["embedding"]) for d in data]


def unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _tokens(text: str) -> set[str]:
    """Words for the keyword fallback: Latin words, and Thai as overlapping 3-character pieces
    (Thai has no spaces between words)."""
    out = set(re.findall(r"[a-z0-9]{2,}", text.lower()))
    for run in re.findall(r"[฀-๿]+", text):
        out.update(run[i:i + 3] for i in range(max(1, len(run) - 2)))
    return out


# -- backends -------------------------------------------------------------------------------
class JsonBackend:
    """Everything in one JSON file (the default, and what tests use)."""

    def __init__(self, path: str | None):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"notes": [], "reminders": [], "episodes": [], "tasks": [], "audit": []}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except (OSError, ValueError) as e:
                log.warning("memory file %s unreadable: %s", path, e)

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def load(self, table: str) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.data.get(table, [])]

    def insert(self, table: str, row: dict) -> dict:
        with self.lock:
            rows = self.data.setdefault(table, [])
            row = {"id": 1 + max([r["id"] for t in self.data.values() for r in t] or [0]), **row}
            rows.append(row)
            if table == "audit":
                del rows[:-5000]
            self._save()
            return row

    def update(self, table: str, row_id: int, fields: dict) -> bool:
        with self.lock:
            for r in self.data.get(table, []):
                if r["id"] == row_id:
                    r.update(fields)
                    self._save()
                    return True
            return False

    def delete(self, table: str, row_id: int) -> bool:
        with self.lock:
            rows = self.data.get(table, [])
            keep = [r for r in rows if r["id"] != row_id]
            self.data[table] = keep
            if len(keep) < len(rows):
                self._save()
            return len(keep) < len(rows)


class PostgresBackend:
    """The same rows in PostgreSQL: one table per kind, each row's fields as jsonb, so the schema
    never needs migrating when a field is added. Works on any PostgreSQL ≥ 12."""

    TABLES = ("notes", "reminders", "episodes", "tasks", "audit")

    def __init__(self, dsn: str):
        import psycopg  # optional extra: pip install "flybot[memory]"
        self.psycopg = psycopg
        self.dsn = dsn
        self.lock = threading.Lock()
        self.conn = psycopg.connect(dsn, autocommit=True)
        with self.conn.cursor() as cur:
            for t in self.TABLES:
                cur.execute(f"CREATE TABLE IF NOT EXISTS flybot_{t} (id bigserial PRIMARY KEY, "
                            f"at timestamptz NOT NULL DEFAULT now(), data jsonb NOT NULL)")

    def _cur(self):
        if self.conn.closed:
            self.conn = self.psycopg.connect(self.dsn, autocommit=True)
        return self.conn.cursor()

    def load(self, table: str) -> list[dict]:
        with self.lock, self._cur() as cur:
            cur.execute(f"SELECT id, data FROM flybot_{table} ORDER BY id")
            return [{"id": i, **d} for i, d in cur.fetchall()]

    def insert(self, table: str, row: dict) -> dict:
        from psycopg.types.json import Jsonb
        with self.lock, self._cur() as cur:
            cur.execute(f"INSERT INTO flybot_{table} (data) VALUES (%s) RETURNING id", (Jsonb(row),))
            return {"id": cur.fetchone()[0], **row}

    def update(self, table: str, row_id: int, fields: dict) -> bool:
        from psycopg.types.json import Jsonb
        with self.lock, self._cur() as cur:
            cur.execute(f"UPDATE flybot_{table} SET data = data || %s WHERE id = %s", (Jsonb(fields), row_id))
            return cur.rowcount > 0

    def delete(self, table: str, row_id: int) -> bool:
        if table == "audit":
            raise ValueError("the audit log is append-only")
        with self.lock, self._cur() as cur:
            cur.execute(f"DELETE FROM flybot_{table} WHERE id = %s", (row_id,))
            return cur.rowcount > 0


def open_backend(dsn: str | None, json_path: str | None):
    if dsn and dsn.startswith(("postgres://", "postgresql://")):
        return PostgresBackend(dsn)
    return JsonBackend(json_path)


# -- episodic memory ------------------------------------------------------------------------
SUMMARIZE = ("Summarise this conversation between the user and you (น้องหวี่, their desk assistant) in "
             "Thai, 1-3 short sentences, as a memory to look up later: who asked what, what was decided "
             "or done, any facts about the user, their work or plans worth remembering. Leave out "
             "greetings and chit-chat. If nothing is worth remembering, reply exactly: -")


class Episodes:
    """Summaries of past conversations, found again by meaning (embeddings) or by words."""

    def __init__(self, backend, embedder: Embedder | None = None, clock: Callable[[], datetime] = datetime.now):
        self.backend, self.embedder, self.clock = backend, embedder, clock
        self.rows = backend.load("episodes")
        self.lock = threading.Lock()

    def add(self, text: str, kind: str = "conversation") -> dict | None:
        text = text.strip()
        if not text or text == "-":
            return None
        row = {"kind": kind, "text": text, "at": self.clock().strftime("%Y-%m-%d %H:%M")}
        if self.embedder:
            try:
                row["embedding"] = self.embedder.embed([text])[0]
            except Exception as e:  # remember it anyway; recall falls back to words for this one
                log.warning("embedding failed: %s", e)
        row = self.backend.insert("episodes", row)
        with self.lock:
            self.rows.append(row)
        return row

    def forget(self, row_id: int) -> bool:
        with self.lock:
            self.rows = [r for r in self.rows if r["id"] != row_id]
        return self.backend.delete("episodes", row_id)

    def recall(self, query: str, k: int = 5) -> list[dict]:
        with self.lock:
            rows = list(self.rows)
        if not rows:
            return []
        q = None
        if self.embedder:
            try:
                q = self.embedder.embed([query])[0]
            except Exception as e:
                log.warning("embedding failed, recalling by words: %s", e)
        words = _tokens(query)
        scored = []
        for r in rows:
            if q is not None and r.get("embedding"):
                score = sum(a * b for a, b in zip(q, r["embedding"]))
            else:
                w = _tokens(r["text"])
                score = len(words & w) / math.sqrt(len(words) * len(w) or 1)
            scored.append((score, r))
        scored.sort(key=lambda s: -s[0])
        return [{"id": r["id"], "at": r["at"], "kind": r["kind"], "text": r["text"], "score": round(s, 3)}
                for s, r in scored[:k] if s > 0]

    def list(self, limit: int = 100) -> list[dict]:
        with self.lock:
            return [{k: v for k, v in r.items() if k != "embedding"} for r in self.rows[-limit:]][::-1]


class Audit:
    """Append-only record of what the assistant did and who asked (phase 2 adds tasks to it)."""

    def __init__(self, backend, clock: Callable[[], datetime] = datetime.now):
        self.backend, self.clock = backend, clock

    def log(self, action: str, **detail) -> None:
        try:
            self.backend.insert("audit", {"at": self.clock().strftime("%Y-%m-%d %H:%M:%S"), "action": action, **detail})
        except Exception as e:
            log.warning("audit log failed: %s", e)

    def recent(self, n: int = 100) -> list[dict]:
        return self.backend.load("audit")[-n:][::-1]
