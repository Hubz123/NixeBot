from __future__ import annotations

import os
import time
import threading
from typing import Optional

_lock = threading.RLock()
_seen: dict[str, float] = {}
_last_gc: float = 0.0

# Bounded in-memory cache (prevents unbounded growth / memory leak patterns).
_ONCE_MAX = int(os.getenv("ONCE_CACHE_MAX_SIZE", "5000") or "5000")
_ONCE_GC_EVERY_SEC = int(os.getenv("ONCE_CACHE_GC_EVERY_SEC", "60") or "60")

# Optional persistent TTL store (survives restarts; reduces dupes after auto-restart).
_PERSIST_ENABLE = (os.getenv("ONCE_PERSIST_ENABLE", "1") or "1").strip() == "1"
_PERSIST_DB_PATH = os.getenv("ONCE_PERSIST_DB_PATH", "data/once_cache.sqlite3").strip() or "data/once_cache.sqlite3"
_PERSIST_MAX_ROWS = int(os.getenv("ONCE_PERSIST_MAX_ROWS", "50000") or "50000")
_PERSIST_GC_EVERY_SEC = int(os.getenv("ONCE_PERSIST_GC_EVERY_SEC", "300") or "300")

_store: Optional[object] = None


def _get_store():
    global _store
    if _store is not None:
        return _store
    if not _PERSIST_ENABLE:
        return None
    try:
        from .once_store import OnceStore  # local import to avoid hard dependency issues
        _store = OnceStore(
            db_path=_PERSIST_DB_PATH,
            max_rows=_PERSIST_MAX_ROWS,
            gc_every_sec=_PERSIST_GC_EVERY_SEC,
        )
        return _store
    except Exception:
        _store = None
        return None


def _mem_gc(now: float) -> None:
    global _last_gc
    if _ONCE_GC_EVERY_SEC > 0 and (now - _last_gc) < _ONCE_GC_EVERY_SEC:
        return

    # Drop expired entries.
    expired = [k for k, v in _seen.items() if v <= now]
    for k in expired:
        _seen.pop(k, None)

    # Hard cap: if still above max, drop earliest expiries first.
    if _ONCE_MAX > 0 and len(_seen) > _ONCE_MAX:
        # sort by expiry ascending (soonest expiry first)
        for k, _ in sorted(_seen.items(), key=lambda kv: kv[1])[: max(0, len(_seen) - _ONCE_MAX)]:
            _seen.pop(k, None)

    _last_gc = now


def once_sync(key: str, ttl: int = 10) -> bool:
    """Return True if first time within TTL; else False.

    This function is intentionally lightweight and safe to call from hot paths.
    It uses a bounded in-memory map and (optionally) a small SQLite TTL store to
    preserve dedupe across restarts without leaking memory.
    """
    if not key:
        return True

    now = time.time()
    ttl_i = int(ttl or 0)
    if ttl_i < 1:
        ttl_i = 1

    # 1) In-memory fast path
    with _lock:
        exp = _seen.get(key)
        if exp and exp > now:
            return False

    # 2) Persistent store check (best-effort)
    st = _get_store()
    if st is not None:
        try:
            exp2 = st.get_expiry(key, now)
            if exp2 and exp2 > now:
                with _lock:
                    _seen[key] = exp2
                    _mem_gc(now)
                return False
        except Exception:
            pass

    # 3) Record new expiry
    new_exp = now + float(ttl_i)
    if st is not None:
        try:
            st.set_expiry(key, new_exp, now)
        except Exception:
            pass

    with _lock:
        _seen[key] = new_exp
        _mem_gc(now)
    return True
