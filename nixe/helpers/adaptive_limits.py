# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import time
import math
import logging
from typing import Optional

log = logging.getLogger(__name__)

def _env_float(key: str, default: float) -> float:
    try:
        v = os.getenv(key)
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)

def _env_int(key: str, default: int) -> int:
    try:
        v = os.getenv(key)
        if v is None or v == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)

# Enable/disable adaptive behavior.
ADAPTIVE_ENABLE = _env_int("NIXE_NET_ADAPTIVE_ENABLE", 1) == 1

# Probe parameters
PROBE_SECONDS = _env_float("NIXE_NET_ADAPTIVE_PROBE_SECONDS", 30.0)
PROBE_TIMEOUT_SECONDS = _env_float("NIXE_NET_ADAPTIVE_PROBE_TIMEOUT_SECONDS", 5.0)

# Throttle policy
BASELINE_THROTTLE_SECONDS = _env_float("NIXE_DISCORD_SEND_THROTTLE_SECONDS", 2.0)
MAX_THROTTLE_SECONDS = _env_float("NIXE_NET_ADAPTIVE_MAX_THROTTLE_SECONDS", 10.0)

# RTT thresholds (ms) => additive throttle seconds.
RTT_THR_1_MS = _env_int("NIXE_NET_ADAPTIVE_RTT_THR1_MS", 800)
RTT_THR_2_MS = _env_int("NIXE_NET_ADAPTIVE_RTT_THR2_MS", 1500)

# Error score decay/step
ERROR_STEP = _env_float("NIXE_NET_ADAPTIVE_ERROR_STEP", 1.0)
ERROR_DECAY_PER_SEC = _env_float("NIXE_NET_ADAPTIVE_ERROR_DECAY_PER_SEC", 0.02)  # ~50s to decay 1pt

# Cloudflare safety (when we detect 1015/HTML 429)
CLOUDFLARE_HARD_COOLDOWN_SECONDS = _env_int("NIXE_DISCORD_CLOUDFLARE_COOLDOWN_SECONDS", 900)

_last_rtt_ms: Optional[float] = None
_error_score: float = 0.0
_last_error_ts: float = 0.0
_cf_cooldown_until: float = 0.0

def set_rtt_ms(rtt_ms: Optional[float]) -> None:
    global _last_rtt_ms
    _last_rtt_ms = rtt_ms

def get_rtt_ms() -> Optional[float]:
    return _last_rtt_ms

def _decay_error_score(now: float) -> None:
    global _error_score, _last_error_ts
    if _last_error_ts <= 0:
        _last_error_ts = now
        return
    dt = max(0.0, now - _last_error_ts)
    if dt <= 0:
        return
    _error_score = max(0.0, _error_score - (ERROR_DECAY_PER_SEC * dt))
    _last_error_ts = now

def record_error(kind: str = "generic") -> None:
    """Record a transient network/API error (timeouts, 429, etc)."""
    global _error_score, _last_error_ts
    now = time.monotonic()
    _decay_error_score(now)
    _error_score += float(ERROR_STEP)
    _last_error_ts = now
    # keep bounded
    _error_score = min(_error_score, 50.0)
    log.debug("[net-adapt] error recorded kind=%s score=%.2f rtt=%s", kind, _error_score, _last_rtt_ms)

def record_cloudflare_1015(reason: str = "") -> None:
    """Engage a hard cooldown when Cloudflare 1015/HTML 429 is detected."""
    global _cf_cooldown_until
    now = time.monotonic()
    _cf_cooldown_until = max(_cf_cooldown_until, now + float(CLOUDFLARE_HARD_COOLDOWN_SECONDS))
    log.warning("[net-adapt] Cloudflare cooldown engaged for %ss reason=%s", CLOUDFLARE_HARD_COOLDOWN_SECONDS, reason)

def is_cloudflare_cooldown_active() -> bool:
    return time.monotonic() < float(_cf_cooldown_until or 0.0)

def get_send_throttle_seconds(default: float) -> float:
    """Return throttle seconds adjusted by RTT & error score."""
    if not ADAPTIVE_ENABLE:
        return float(default)

    now = time.monotonic()
    _decay_error_score(now)

    # Hard stop during CF cooldown: caller can choose to drop; we just return a big throttle.
    if is_cloudflare_cooldown_active():
        return float(MAX_THROTTLE_SECONDS)

    add = 0.0
    rtt = _last_rtt_ms
    if rtt is not None:
        if rtt >= RTT_THR_2_MS:
            add += 2.0
        elif rtt >= RTT_THR_1_MS:
            add += 1.0

    # Convert error score to additive throttle with diminishing returns.
    # score 0..10 => add ~0..3s, capped.
    add += min(3.0, math.log1p(max(0.0, _error_score)) * 1.25)

    base = float(default if default is not None else BASELINE_THROTTLE_SECONDS)
    thr = min(float(MAX_THROTTLE_SECONDS), max(0.0, base + add))
    return thr
