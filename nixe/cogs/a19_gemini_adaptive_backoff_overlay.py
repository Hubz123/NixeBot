from __future__ import annotations

"""
[a19-gemini-backoff]
Adaptive backoff for classify_lucky_pull_bytes on transient errors (rate limit, 503, disconnect, timeout).
Additive; NOOP if helper not found or already patched.
"""

import os
import asyncio
import logging
import random

log = logging.getLogger(__name__)

def _env_bool(name: str, default: bool=True) -> bool:
    v = str(os.getenv(name, "")).strip().lower()
    if v in ("1","true","yes","y","on"): return True
    if v in ("0","false","no","n","off"): return False
    return default

def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip())
    except Exception:
        return default

def _is_transient_error(e: Exception) -> bool:
    s = repr(e).lower()
    return any(tok in s for tok in [
        "429", "rate", "quota", "too many requests",
        "503", "server disconnected", "timeout", "temporarily",
        "connection reset", "server error",
    ])

async def setup(bot):
    if not _env_bool("GEMINI_ADAPTIVE_BACKOFF_ENABLE", True):
        log.warning("[gemini-backoff] disabled via env")
        return

    try:
        import nixe.helpers.gemini_bridge as gb
    except Exception as e:
        log.warning(f"[gemini-backoff] import failed: {e}")
        return

    fn = getattr(gb, "classify_lucky_pull_bytes", None)
    if not callable(fn):
        log.warning("[gemini-backoff] classify_lucky_pull_bytes not found; NOOP")
        return
    if getattr(fn, "_nixe_backoff_patched", False):
        return

    max_retries = int(_env_float("GEMINI_BACKOFF_MAX_RETRIES", 2))
    base_delay = _env_float("GEMINI_BACKOFF_BASE_SEC", 0.6)
    max_delay = _env_float("GEMINI_BACKOFF_MAX_SEC", 6.0)
    jitter = _env_float("GEMINI_BACKOFF_JITTER_SEC", 0.35)

    async def wrapped(*args, **kwargs):
        attempt = 0
        last_exc = None
        while attempt <= max_retries:
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                last_exc = e
                if not _is_transient_error(e) or attempt >= max_retries:
                    raise
                delay = min(max_delay, base_delay * (2 ** attempt)) + random.uniform(0, jitter)
                log.warning(f"[gemini-backoff] transient error; retry {attempt+1}/{max_retries} in {delay:.2f}s: {e}")
                await asyncio.sleep(delay)
                attempt += 1
        raise last_exc

    setattr(wrapped, "_nixe_backoff_patched", True)
    gb.classify_lucky_pull_bytes = wrapped
    log.warning(f"[gemini-backoff] patched classify_lucky_pull_bytes retries={max_retries}")
