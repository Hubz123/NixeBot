from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Optional


class OnceStore:
    """Tiny SQLite-backed TTL store for once_sync().

    Table schema:
      once(k TEXT PRIMARY KEY, exp REAL, ts REAL)

    - exp: expiry unix timestamp (seconds)
    - ts: insertion/update unix timestamp (seconds) for size-based GC
    """

    def __init__(self, db_path: str, max_rows: int = 50000, gc_every_sec: int = 300):
        self.db_path = (db_path or "data/once_cache.sqlite3").strip()
        self.max_rows = int(max_rows or 0)
        self.gc_every_sec = int(gc_every_sec or 0)
        self._lock = threading.RLock()
        self._last_gc = 0.0

        # Ensure parent directory exists.
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False because Discord bots are async; we protect with our own lock.
        conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS once (k TEXT PRIMARY KEY, exp REAL NOT NULL, ts REAL NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_once_exp ON once(exp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_once_ts ON once(ts)")
            conn.commit()

    def get_expiry(self, key: str, now: Optional[float] = None) -> Optional[float]:
        if not key:
            return None
        now = float(time.time() if now is None else now)
        with self._lock:
            try:
                with self._connect() as conn:
                    row = conn.execute("SELECT exp FROM once WHERE k=?", (key,)).fetchone()
                    if not row:
                        self._gc(conn, now)
                        return None
                    exp = float(row[0] or 0.0)
                    if exp <= now:
                        conn.execute("DELETE FROM once WHERE k=?", (key,))
                        conn.commit()
                        self._gc(conn, now)
                        return None
                    self._gc(conn, now)
                    return exp
            except Exception:
                return None

    def set_expiry(self, key: str, exp: float, now: Optional[float] = None) -> None:
        if not key:
            return
        now = float(time.time() if now is None else now)
        exp = float(exp or 0.0)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO once(k, exp, ts) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET exp=excluded.exp, ts=excluded.ts",
                    (key, exp, now),
                )
                conn.commit()
                self._gc(conn, now)

    def _gc(self, conn: sqlite3.Connection, now: float) -> None:
        # Time-based throttle
        if self.gc_every_sec > 0 and (now - self._last_gc) < self.gc_every_sec:
            return

        # Remove expired.
        try:
            conn.execute("DELETE FROM once WHERE exp <= ?", (now,))
        except Exception:
            pass

        # Size cap (best-effort). Delete oldest by ts.
        if self.max_rows and self.max_rows > 0:
            try:
                row = conn.execute("SELECT COUNT(1) FROM once").fetchone()
                n = int(row[0] or 0) if row else 0
                if n > self.max_rows:
                    extra = n - self.max_rows
                    conn.execute(
                        "DELETE FROM once WHERE k IN (SELECT k FROM once ORDER BY ts ASC LIMIT ?)",
                        (extra,),
                    )
            except Exception:
                pass

        try:
            conn.commit()
        except Exception:
            pass

        self._last_gc = now
