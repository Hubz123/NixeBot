# -*- coding: utf-8 -*-
"""nixe.helpers.safe_delete

Centralized, rate-limit-friendly message deletion helper.

This module exists to prevent Discord 429 spikes caused by concurrent deletes
from multiple cogs. It serializes deletes through a single queue worker and
applies a minimum interval between DELETE calls.

Public API:
    await safe_delete(message, label="...", delay=0.0, reason="...")

Notes on compatibility:
- Some discord.py versions support Message.delete(reason=...), some don't.
  We attempt with reason and fall back without it.
- Older / mixed patches may call the internal helper with unexpected kwargs.
  `_guarded_delete` accepts **_ignored to avoid crashes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional, Tuple

log = logging.getLogger("nixe.helpers.safe_delete")

try:
    import discord  # type: ignore
except Exception:  # pragma: no cover
    discord = None  # type: ignore


def _is_render() -> bool:
    for k in ("RENDER", "RENDER_INSTANCE_ID", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL", "RENDER_EXTERNAL_HOSTNAME"):
        if os.getenv(k):
            return True
    return False


def _env_int(name: str, default: int) -> int:
    try:
        v = int(str(os.getenv(name, "")).strip())
        return v
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        v = float(str(os.getenv(name, "")).strip())
        return v
    except Exception:
        return default


_QUEUE_MAXSIZE = max(10, _env_int("NIXE_DELETE_QUEUE_MAXSIZE", _env_int("NIXE_DELETE_QUEUE_MAX", 2000)))
_MIN_INTERVAL_SEC = max(0.0, _env_float("NIXE_DELETE_MIN_INTERVAL_SEC", 0.80 if _is_render() else 0.35))
_MAX_RETRIES = max(0, _env_int("NIXE_DELETE_MAX_RETRIES", 3))
_RETRY_BASE_SEC = max(0.1, _env_float("NIXE_DELETE_RETRY_BASE_SEC", 1.0))

# Queue item: (message, delay_seconds, label, reason)
_Item = Tuple[Any, float, str, Optional[str]]
_queue: "asyncio.Queue[_Item]" = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

_worker_task: Optional[asyncio.Task] = None
_last_delete_ts: float = 0.0


def _ensure_worker() -> None:
    """Start a single worker task on the current running loop."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _worker_task = loop.create_task(_delete_worker(), name="nixe.safe_delete.worker")


async def safe_delete(
    message: Any,
    *,
    label: str = "",
    delay: float = 0.0,
    reason: Optional[str] = None,
) -> bool:
    """Enqueue a message deletion (serialized, rate-limit-friendly)."""
    if message is None:
        return False

    _ensure_worker()

    try:
        _queue.put_nowait((message, float(delay or 0.0), str(label or ""), reason))
        return True
    except asyncio.QueueFull:
        # Best-effort fallback: do direct delete with pacing.
        try:
            await asyncio.sleep(_MIN_INTERVAL_SEC + max(0.0, float(delay or 0.0)))
            await _guarded_delete(message, label=label or "fallback", reason=reason)
            return True
        except Exception:
            return False


async def _delete_worker() -> None:
    """Background worker that processes the delete queue forever."""
    global _last_delete_ts
    while True:
        message, delay, label, reason = await _queue.get()
        try:
            if delay and delay > 0:
                await asyncio.sleep(delay)

            # Pace deletes.
            now = time.monotonic()
            wait = (_last_delete_ts + _MIN_INTERVAL_SEC) - now
            if wait > 0:
                await asyncio.sleep(wait)

            await _guarded_delete(message, label=label, reason=reason)
            _last_delete_ts = time.monotonic()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Never let the worker die.
            _last_delete_ts = time.monotonic()
            mid = getattr(message, "id", "?")
            ch = getattr(getattr(message, "channel", None), "id", "?")
            log.warning("[safe_delete] worker error label=%s mid=%s ch=%s err=%r", label, mid, ch, e)
        finally:
            try:
                _queue.task_done()
            except Exception:
                pass


async def _guarded_delete(
    message: Any,
    *,
    label: str = "",
    reason: Optional[str] = None,
    **_ignored: Any,
) -> bool:
    """Delete a message with robust exception handling and retry on 429.

    Accepts **_ignored to remain compatible with any older wiring that passes
    unexpected kwargs.
    """
    mid = getattr(message, "id", "?")
    ch = getattr(getattr(message, "channel", None), "id", "?")

    delete_coro = getattr(message, "delete", None)
    if delete_coro is None:
        log.warning("[safe_delete] no delete() label=%s mid=%s ch=%s", label, mid, ch)
        return False

    async def _do_delete(with_reason: bool) -> None:
        if with_reason and reason:
            await delete_coro(reason=reason)  # type: ignore[misc]
        else:
            await delete_coro()  # type: ignore[misc]

    attempt = 0
    while True:
        attempt += 1
        try:
            # Try with reason kw first, then fall back if unsupported.
            try:
                await _do_delete(with_reason=True)
            except TypeError:
                await _do_delete(with_reason=False)
            return True

        except Exception as e:
            # If discord isn't imported, just log generically.
            if discord is None:
                log.warning("[safe_delete] delete error label=%s mid=%s ch=%s err=%r", label, mid, ch, e)
                return False

            Forbidden = getattr(discord, "Forbidden", ())
            NotFound = getattr(discord, "NotFound", ())
            HTTPException = getattr(discord, "HTTPException", ())

            if Forbidden and isinstance(e, Forbidden):
                log.warning("[safe_delete] forbidden label=%s mid=%s ch=%s", label, mid, ch)
                return False

            if NotFound and isinstance(e, NotFound):
                # Already gone.
                return True

            if HTTPException and isinstance(e, HTTPException):
                status = getattr(e, "status", None)
                if status == 429 and attempt <= max(1, _MAX_RETRIES):
                    # Best-effort backoff; use retry_after if exposed.
                    backoff = _RETRY_BASE_SEC * attempt
                    retry_after = getattr(e, "retry_after", None)
                    if isinstance(retry_after, (int, float)) and retry_after > 0:
                        backoff = max(backoff, float(retry_after))
                    backoff = max(0.5, min(10.0, float(backoff)))
                    log.warning(
                        "[safe_delete] 429 label=%s mid=%s ch=%s backoff=%.2fs attempt=%s/%s",
                        label,
                        mid,
                        ch,
                        backoff,
                        attempt,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(backoff)
                    continue

            # Generic fallback.
            log.warning("[safe_delete] delete error label=%s mid=%s ch=%s err=%r", label, mid, ch, e)
            return False
