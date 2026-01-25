# -*- coding: utf-8 -*-
# a00c_render_memory_guard_overlay.py
#
# Render Free plan hard-kills processes that exceed the memory limit (often 512MB),
# resulting in "restart without logs". This cog adds best-effort guardrails:
# - reads configured cap (runtime_env.json via env-hybrid)
# - watches RSS and exits before OOM-kill
# - purges known cache files every N days (default: 3) to prevent long-lived growth
from __future__ import annotations

import asyncio
import gc
import logging
import os
import signal
import time
from pathlib import Path
from typing import Optional, List

log = logging.getLogger("nixe.cogs.render_mem_guard")


def _is_render() -> bool:
    for k in ("RENDER", "RENDER_SERVICE_ID", "RENDER_INSTANCE_ID", "RENDER_EXTERNAL_URL"):
        if os.getenv(k):
            return True
    return False


def _read_cgroup_limit_bytes() -> Optional[int]:
    # cgroup v2
    for p in ("/sys/fs/cgroup/memory.max",):
        try:
            s = Path(p).read_text().strip()
            if not s or s == "max":
                continue
            v = int(s)
            # Ignore absurdly high limits (no effective cap)
            if v > 0 and v < (1 << 60):
                return v
        except Exception:
            pass
    # cgroup v1
    for p in ("/sys/fs/cgroup/memory/memory.limit_in_bytes",):
        try:
            s = Path(p).read_text().strip()
            v = int(s)
            if v > 0 and v < (1 << 60):
                # v1 often reports very large value when unlimited; ignore > 8TB
                if v > (8 * (1 << 40)):
                    continue
                return v
        except Exception:
            pass
    return None


def _read_rss_mb() -> Optional[float]:
    # Prefer /proc (Linux, Render).
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    kb = float(parts[1])
                    return kb / 1024.0
    except Exception:
        pass
    # Fallback: resource ru_maxrss (not current RSS; still better than nothing)
    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF)
        # On Linux ru_maxrss is KB
        return float(getattr(ru, "ru_maxrss", 0.0)) / 1024.0
    except Exception:
        return None


def _parse_paths(raw: str) -> List[Path]:
    out: List[Path] = []
    for part in (raw or "").replace(",", ";").split(";"):
        s = (part or "").strip()
        if not s:
            continue
        out.append(Path(s))
    return out


def _cap_mb() -> int:
    cap = int(os.getenv("NIXE_RAM_CAP_MB", "0") or "0")
    if cap <= 0:
        return 0
    if os.getenv("NIXE_RAM_USE_CGROUP", "1") == "1":
        cg = _read_cgroup_limit_bytes()
        if cg:
            cg_mb = int(cg / (1024 * 1024))
            if cg_mb > 0:
                cap = min(cap, cg_mb)
    return cap


async def _maybe_exit_for_rss(rss_mb: float, exit_mb: int, cap_mb: int) -> None:
    if exit_mb <= 0:
        return
    if rss_mb < float(exit_mb):
        return

    msg = f"[mem-guard] RSS={rss_mb:.1f}MB >= exit={exit_mb}MB (cap={cap_mb}MB). Exiting to avoid OOM-kill."
    log.error(msg)

    # Best-effort: flush caches & GC once before exit (may or may not reduce RSS).
    try:
        from nixe.helpers import lpg_cache
        getattr(lpg_cache, "_CACHE", {}).clear()
    except Exception:
        pass
    try:
        gc.collect()
    except Exception:
        pass

    # Exit fast to let Render restart cleanly.
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        raise SystemExit(1)


async def _watchdog_loop() -> None:
    if not _is_render():
        return
    cap = _cap_mb()
    if cap <= 0:
        return

    check_sec = int(os.getenv("NIXE_RAM_CHECK_SEC", "10") or "10")
    check_sec = max(3, min(check_sec, 60))

    # Exit threshold defaults to cap - 32MB, but can be overridden.
    exit_mb = int(os.getenv("NIXE_RAM_EXIT_MB", str(max(1, cap - 32))) or str(max(1, cap - 32)))
    exit_mb = max(1, min(exit_mb, cap))

    log.warning("[mem-guard] enabled on Render. cap=%dMB exit=%dMB check=%ds", cap, exit_mb, check_sec)

    while True:
        await asyncio.sleep(check_sec)
        rss = _read_rss_mb()
        if rss is None:
            continue
        await _maybe_exit_for_rss(rss, exit_mb, cap)


def _purge_marker_path() -> Path:
    return Path("data/.nixe_cache_purge_ts")


def _read_last_purge_ts() -> float:
    p = _purge_marker_path()
    try:
        return float(p.read_text().strip())
    except Exception:
        return 0.0


def _write_last_purge_ts(ts: float) -> None:
    p = _purge_marker_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(float(ts)))
    except Exception:
        pass


def _purge_files(paths: List[Path]) -> int:
    removed = 0
    for p in paths:
        try:
            if p.exists():
                p.unlink()
                removed += 1
        except Exception:
            pass
    return removed


async def _cache_purge_loop() -> None:
    # Cache purging is useful on both Render and miniPC; but on Render it prevents long-lived growth.
    days = int(os.getenv("NIXE_CACHE_PURGE_DAYS", "0") or "0")
    if days <= 0:
        return

    every = 3600  # check hourly
    paths = _parse_paths(os.getenv("NIXE_CACHE_PURGE_PATHS", "data/once_cache.sqlite3;data/once_cache.sqlite3-wal;data/once_cache.sqlite3-shm"))

    # Initialize marker if missing, to avoid purging immediately on boot.
    now = time.time()
    last = _read_last_purge_ts()
    if last <= 0:
        _write_last_purge_ts(now)
        last = now

    period = float(days) * 86400.0
    log.warning("[cache-purge] enabled. every=%dd paths=%d", days, len(paths))

    while True:
        await asyncio.sleep(every)
        now = time.time()
        last = _read_last_purge_ts()
        if last <= 0:
            _write_last_purge_ts(now)
            continue
        if (now - last) < period:
            continue

        removed = _purge_files(paths)

        # Also prune in-memory caches (best-effort)
        try:
            from nixe.helpers import lpg_cache
            getattr(lpg_cache, "_CACHE", {}).clear()
        except Exception:
            pass
        try:
            from nixe.cogs import phish_groq_guard
            getattr(phish_groq_guard, "_SEEN_URL_EXP", {}).clear()
        except Exception:
            pass

        try:
            gc.collect()
        except Exception:
            pass

        _write_last_purge_ts(now)
        log.warning("[cache-purge] done. removed_files=%d", removed)


async def setup(bot):
    # Start background tasks (non-blocking)
    try:
        asyncio.create_task(_watchdog_loop())
    except Exception:
        pass
    try:
        asyncio.create_task(_cache_purge_loop())
    except Exception:
        pass
