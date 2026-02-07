# -*- coding: utf-8 -*-
"""nixe.helpers.memory_sweeper

Render Free plan enforces a hard memory ceiling (commonly 512MB). When a Python
process exceeds the limit, the platform kills the process abruptly, often
without a useful traceback.

This helper provides *best-effort* in-process memory relief by clearing known
caches (Discord message cache + Nixe helpers), forcing GC, and (on Linux)
requesting allocator trimming.

This is not a hard memory cap. It is a practical mitigation to reduce the
chance of platform OOM restarts.
"""

from __future__ import annotations

import gc
import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

# Avoid log spam on Render: the sweeper can run every few seconds when memory is
# under pressure. Only emit WARNING when we actually freed memory or cleared
# caches; otherwise stay quiet (or at most DEBUG, rate-limited).
_LAST_NOOP_DEBUG_TS: float = 0.0
_NOOP_DEBUG_EVERY_S: float = 60.0

_LAST_WARN_TS: float = 0.0
_WARN_EVERY_S: float = 60.0  # at most 1 WARNING per minute


def rss_mb() -> float:
    """Return resident set size (RSS) in MB, best-effort."""
    # Preferred: psutil
    try:
        import psutil  # type: ignore

        p = psutil.Process(os.getpid())
        return float(p.memory_info().rss) / (1024.0 * 1024.0)
    except Exception:
        pass

    # Fallback: resource (ru_maxrss). Note: on Linux ru_maxrss is KB.
    try:
        import resource  # type: ignore

        r = resource.getrusage(resource.RUSAGE_SELF)
        kb = float(getattr(r, "ru_maxrss", 0.0) or 0.0)
        # Linux: KB; macOS: bytes. Render is Linux.
        if kb > 10_000_000:
            return kb / (1024.0 * 1024.0)
        return kb / 1024.0
    except Exception:
        return 0.0


def _malloc_trim() -> None:
    """Best-effort allocator trim on Linux/glibc."""
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        return


def _clear_discord_message_cache(bot: Any) -> int:
    """Clear discord.py message cache if present. Returns number removed."""
    try:
        state = getattr(bot, "_connection", None)
        msgs = getattr(state, "_messages", None)
        if msgs is None:
            return 0
        n = len(msgs)
        try:
            msgs.clear()
        except Exception:
            # Some versions use deque-like; rebind to empty list/deque is unsafe.
            return 0
        return int(n)
    except Exception:
        return 0


def _clear_nixe_caches(aggressive: bool = False) -> None:
    # LPG cache (image score cache)
    try:
        from nixe.helpers import lpg_cache

        lpg_cache.clear()  # type: ignore[attr-defined]
    except Exception:
        pass

    # Once in-memory dedupe
    try:
        from nixe.helpers import once

        once.purge_memory()  # type: ignore[attr-defined]
    except Exception:
        pass

    # Phish evidence cache (in-memory)
    try:
        from nixe.helpers import phish_evidence_cache

        phish_evidence_cache.clear_all()  # type: ignore[attr-defined]
    except Exception:
        pass

    # Aggressive: also drop cached regex compiles in this module set if any.
    if aggressive:
        try:
            import re

            re.purge()
        except Exception:
            pass


def sweep(bot: Optional[Any] = None, *, aggressive: bool = False) -> None:
    """Attempt to free memory by clearing caches + GC.

    - aggressive=False: safe fast sweep
    - aggressive=True: deeper sweep
    """

    t0 = time.time()
    before = rss_mb()

    cleared_msgs = 0
    if bot is not None:
        cleared_msgs = _clear_discord_message_cache(bot)

    _clear_nixe_caches(aggressive=aggressive)

    # Force GC; do twice to handle cyclic garbage.
    try:
        gc.collect()
        if aggressive:
            gc.collect()
    except Exception:
        pass

    # Ask allocator to return free pages to OS (best-effort)
    if aggressive:
        _malloc_trim()

    after = rss_mb()
    dt = int((time.time() - t0) * 1000)

    freed = before - after

    # Only warn when we *actually* freed meaningful RSS (to avoid log spam).
    # Clearing discord.py message cache does not always reduce RSS immediately.
    warn = (freed >= 1.0)

    if warn:
        global _LAST_WARN_TS
        now = time.monotonic()
        if (now - _LAST_WARN_TS) >= _WARN_EVERY_S:
            _LAST_WARN_TS = now
            log.warning(
                "[mem-sweep] aggressive=%s cleared_msgs=%d rss_mb %.1f -> %.1f (freed=%.1fMB dt=%dms)",
                aggressive,
                cleared_msgs,
                before,
                after,
                freed,
                dt,
            )
        return

    # No-op / tiny change: keep logs quiet to avoid spam.
    global _LAST_NOOP_DEBUG_TS
    now = time.monotonic()
    if (now - _LAST_NOOP_DEBUG_TS) >= _NOOP_DEBUG_EVERY_S:
        _LAST_NOOP_DEBUG_TS = now
        log.debug(
            "[mem-sweep] aggressive=%s cleared_msgs=%d rss_mb %.1f -> %.1f (freed=%.1fMB dt=%dms)",
            aggressive,
            cleared_msgs,
            before,
            after,
            freed,
            dt,
        )
