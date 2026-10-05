"""Live queue load from the hospital's Q4U queue system (MySQL ``app_queue``).

Only aggregate counts are ever selected: tickets issued per service point per minute
(``COUNT(*) ... GROUP BY``). No patient column (hn, vn, names) is read. Give the twin a
read-only account limited to ``app_queue.q4u_queue`` and ``app_queue.q4u_service_points``.

    FLYBOT_HIS_DSN=mysql://flybot_ro:***@his-db:3306/app_queue python -m flybot.twin --live
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from urllib.parse import unquote, urlparse

import numpy as np

log = logging.getLogger(__name__)

NOT_CANCELLED = "(is_cancel IS NULL OR is_cancel != 'Y')"


def parse_dsn(dsn: str) -> dict:
    u = urlparse(dsn)
    if u.scheme not in ("mysql", "mysql+pymysql"):
        raise ValueError("expected mysql://user:password@host:port/database")
    return {"host": u.hostname, "port": u.port or 3306, "user": unquote(u.username or ""),
            "password": unquote(u.password or ""), "database": (u.path or "/app_queue").lstrip("/") or "app_queue"}


class Q4USource:
    def __init__(self, dsn: str, lookback_days: int = 28, timeout: float = 10.0):
        self.params = {**parse_dsn(dsn), "connect_timeout": timeout, "read_timeout": timeout, "charset": "utf8mb4"}
        self.lookback_days = lookback_days
        self._conn = None

    def _query(self, sql: str, args: tuple = ()) -> list[tuple]:
        import pymysql

        for attempt in (1, 2):  # reconnect once if the server dropped an idle connection
            try:
                if self._conn is None:
                    self._conn = pymysql.connect(**self.params, autocommit=True)
                with self._conn.cursor() as cur:
                    cur.execute(sql, args)
                    return list(cur.fetchall())
            except pymysql.err.OperationalError:
                self._conn = None
                if attempt == 2:
                    raise
        return []

    def points(self, today: date) -> list[tuple[int, str]]:
        """Service points that issued tickets in the look-back window (busiest first)."""
        rows = self._query(
            "SELECT q.service_point_id, sp.service_point_name, COUNT(*) AS n "
            "FROM q4u_queue q JOIN q4u_service_points sp ON sp.service_point_id = q.service_point_id "
            f"WHERE q.date_serv >= %s - INTERVAL %s DAY AND q.date_serv <= %s AND {NOT_CANCELLED} "
            "GROUP BY q.service_point_id, sp.service_point_name ORDER BY n DESC",
            (today, self.lookback_days, today))
        return [(int(sp), str(name)) for sp, name, _ in rows]

    def capacities(self, today: date) -> dict[int, float]:
        """Patients per minute each point copes with: 85th percentile of its 30-min rates on past days."""
        rows = self._query(
            "SELECT service_point_id, date_serv, FLOOR((HOUR(time_serv) * 60 + MINUTE(time_serv)) / 30) AS slot, "
            "COUNT(*) FROM q4u_queue "
            f"WHERE date_serv >= %s - INTERVAL %s DAY AND date_serv < %s AND {NOT_CANCELLED} "
            "GROUP BY service_point_id, date_serv, slot",
            (today, self.lookback_days, today))
        rates: dict[int, list[float]] = {}
        for sp, _day, _slot, n in rows:
            rates.setdefault(int(sp), []).append(n / 30.0)
        return {sp: max(0.05, float(np.percentile(r, 85))) for sp, r in rates.items()}

    def arrivals(self, day: date, from_minute: int, to_minute: int) -> dict[int, dict[int, int]]:
        """Tickets issued per point per minute in [from_minute, to_minute) of ``day``."""
        rows = self._query(
            "SELECT service_point_id, HOUR(time_serv) * 60 + MINUTE(time_serv) AS m, COUNT(*) FROM q4u_queue "
            f"WHERE date_serv = %s AND time_serv >= SEC_TO_TIME(%s * 60) AND time_serv < SEC_TO_TIME(%s * 60) "
            f"AND {NOT_CANCELLED} GROUP BY service_point_id, m",
            (day, from_minute, to_minute))
        out: dict[int, dict[int, int]] = {}
        for sp, m, n in rows:
            out.setdefault(int(sp), {})[int(m)] = int(n)
        return out


def now_minute() -> tuple[date, int]:
    """Today and minute of day in the hospital's time zone (time_serv is stored as local time)."""
    from zoneinfo import ZoneInfo

    t = datetime.now(ZoneInfo(os.environ.get("FLYBOT_HIS_TZ", "Asia/Bangkok")))
    return t.date(), t.hour * 60 + t.minute

