# -*- coding: utf-8 -*-
"""nixe.helpers.safe_delete

Centralized, rate-limit-friendly message deletion helper.

Goal:
- Prevent Discord 429 spikes caused by concurrent deletes from multiple cogs.
- Provide a single async API: `await safe_delete(message, ...)` which enqueues deletions.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional, Tuple

try:
    import discord  # type: ignore
except Exception:  # pragma: no cover
    discord = None  # type: ignore

log = logging.getLogger(__name__)

# Queue item: (message, delay_seconds, label, reason)
_Item = Tuple[object, float, str, str]

_queue: "asyncio.Queue[_Item]" = asyncio.Queue(maxsize=5000)
_started: bool = False
_worker_task: Optional[asyncio.Task] = None
_last_delete_ts: float = 0.0


def _is_render() -> bool:
    # Render commonly sets these
    for k in ("RENDER", "RENDER_SERVICE_ID", "RENDER_INSTANCE_ID", "RENDER_EXTERNAL_URL"):
        if os.getenv(k):
            return True
    return False


def _min_interval() -> float:
    # Allow explicit override
    v = os.getenv("NIXE_DELETE_MIN_INTERVAL_SEC") or ""
    try:
        if v.strip():
            return max(0.0, float(v))
    except Exception:
        pass

    # Defaults: Render is more conservative
    return 0.75 if _is_render() else 0.25


_MIN_INTERVAL_SEC: float = _min_interval()


def _ensure_started() -> None:
    """Start the background worker once per process."""
    global _started, _worker_task
    if _started:
        return
    _started = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop at import time; `safe_delete` will call again later.
        _started = False
        return
    _worker_task = loop.create_task(_worker(), name="nixe.safe_delete.worker")


async def _worker() -> None:
    global _last_delete_ts
    while True:
        msg, delay, label, reason = await _queue.get()
        try:
            if delay > 0:
                await asyncio.sleep(delay)

            # pace deletes
            now = time.monotonic()
            wait = _MIN_INTERVAL_SEC - (now - _last_delete_ts)
            if wait > 0:
                await asyncio.sleep(wait)

            # If discord isn't available, just drop safely.
            if discord is None:
                _last_delete_ts = time.monotonic()
                continue

            # best-effort delete with one retry on 429
            await _delete_one(msg, label=label, reason=reason)
            _last_delete_ts = time.monotonic()

        except Exception as e:
            # never let the worker die
            _last_delete_ts = time.monotonic()
            log.warning("[safe_delete] worker error label=%s err=%r", label, e)
        finally:
            try:
                _queue.task_done()
            except Exception:
                pass


async def _delete_one(msg: object, *, label: str, reason: str) -> None:
    """Delete once; handles common discord.py exceptions."""
    mid = getattr(msg, "id", "?")
    ch = getattr(getattr(msg, "channel", None), "id", "?")
    try:
        await getattr(msg, "delete")(reason=reason or None)
    except getattr(discord, "NotFound"):
        return
    except getattr(discord, "Forbidden"):
        log.warning("[safe_delete] forbidden label=%s mid=%s ch=%s", label, mid, ch)
        return
    except getattr(discord, "HTTPException") as e:
        st = int(getattr(e, "status", 0) or 0)
        if st == 429:
            # Try to honor retry_after if present, else conservative backoff.
            ra = getattr(e, "retry_after", None)
            try:
                sleep_s = float(ra) if ra is not None else 1.5
            except Exception:
                sleep_s = 1.5
            log.warning("[safe_delete] 429 label=%s mid=%s ch=%s backoff=%.2fs", label, mid, ch, sleep_s)
            await asyncio.sleep(max(0.5, min(10.0, sleep_s)))
            try:
                await getattr(msg, "delete")(reason=reason or None)
            except Exception as e2:
                log.warning("[safe_delete] retry failed label=%s mid=%s ch=%s err=%r", label, mid, ch, e2)
            return
        log.warning("[safe_delete] http error label=%s mid=%s ch=%s status=%s err=%r", label, mid, ch, st, e)
        return
    except Exception as e:
        log.warning("[safe_delete] delete error label=%s mid=%s ch=%s err=%r", label, mid, ch, e)
        return


async def safe_delete(message: object, *, delay: float = 0.0, label: str = "", reason: str = "") -> bool:
    """Queue a message for deletion.

    Returns True if queued (or deleted via fallback), False otherwise.
    """
    global _started
    if not _started:
        _ensure_started()

    # If still not started (no running loop), fall back to direct delete best-effort.
    if not _started:
        try:
            await asyncio.sleep(max(0.0, float(delay or 0.0)))
            await getattr(message, "delete")(reason=reason or None)
            return True
        except Exception:
            return False

    try:
        _queue.put_nowait((message, float(delay or 0.0), str(label or ""), str(reason or "")))
        return True
    except asyncio.QueueFull:
        # Fallback: attempt direct delete (best-effort) with pacing.
        try:
            await asyncio.sleep(_MIN_INTERVAL_SEC + max(0.0, float(delay or 0.0)))
            await getattr(message, "delete")(reason=reason or None)
            return True
        except Exception:
            return False
