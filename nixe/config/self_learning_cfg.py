# -*- coding: utf-8 -*-
"""
NIXE self_learning_cfg (scheduler alias fix)
- Provides PHASH_FIRST_DELAY_SECONDS and PHASH_INTERVAL_SECONDS
  as aliases to PHASH_WATCH_FIRST_DELAY and PHASH_WATCH_INTERVAL.
- Also exports the usual IDs and inbox string/ID.
All values can be overridden via ENV.
"""
from __future__ import annotations
import os

def _to_int(v, d):
    try:
        return int(v)
    except Exception:
        return int(d)

# ---- IDs (with fallback) ----
try:
    from ..config_ids import (
        LOG_BOTPHISHING as _LOG_CH,
        THREAD_NIXE_DB as _THREAD_DB,
        THREAD_IMAGEPHISH as _THREAD_IMG,
        BAN_BRAND_NAME as BAN_BRAND_NAME,
        TESTBAN_CHANNEL_ID as TESTBAN_CHANNEL_ID,
    )
except Exception:
    _LOG_CH = 1431178130155896882
    _THREAD_DB = 1431192568221270108
    _THREAD_IMG = 1409949797313679492
    BAN_BRAND_NAME = "external"
    TESTBAN_CHANNEL_ID = 936690788946030613

LOG_CHANNEL_ID = _to_int(os.getenv("LOG_CHANNEL_ID") or _LOG_CH, _LOG_CH)

THREAD_NIXE = _to_int(os.getenv("THREAD_NIXE") or _THREAD_DB, _THREAD_DB)
THREAD_NIXE_DB = THREAD_NIXE

THREAD_IMAGEPHISH = _to_int(os.getenv("THREAD_IMAGEPHISH") or _THREAD_IMG, _THREAD_IMG)
THREAD_IMAGEPHISING = THREAD_IMAGEPHISH  # alias

# --- inbox compat ---
# For guards that expect a name list:
_default_inbox_names = "imagephising,imagephish"
PHASH_INBOX_THREAD = os.getenv("PHASH_INBOX_THREAD") or _default_inbox_names
# For watchers that expect a numeric ID:
PHASH_INBOX_THREAD_ID = _to_int(os.getenv("PHASH_INBOX_THREAD_ID") or THREAD_IMAGEPHISH, THREAD_IMAGEPHISH)

# --- scheduler values ---
PHASH_WATCH_FIRST_DELAY   = _to_int(os.getenv("PHASH_WATCH_FIRST_DELAY") or 5, 5)
PHASH_WATCH_INTERVAL      = _to_int(os.getenv("PHASH_WATCH_INTERVAL") or 30, 30)
# aliases expected by some cogs
PHASH_FIRST_DELAY_SECONDS = _to_int(os.getenv("PHASH_FIRST_DELAY_SECONDS") or PHASH_WATCH_FIRST_DELAY, PHASH_WATCH_FIRST_DELAY)
PHASH_INTERVAL_SECONDS    = _to_int(os.getenv("PHASH_INTERVAL_SECONDS") or PHASH_WATCH_INTERVAL, PHASH_WATCH_INTERVAL)

# --- board / limits ---
PHASH_DB_MARKER      = os.getenv("PHASH_DB_MARKER") or "[phash-db-board]"
PHASH_LOG_SCAN_LIMIT = _to_int(os.getenv("PHASH_LOG_SCAN_LIMIT") or 200, 200)
PHASH_HAMMING_MAX    = _to_int(os.getenv("PHASH_HAMMING_MAX") or 0, 0)

# --- healthz compat (some web helpers import these here) ---
NIXE_HEALTHZ_PATH = os.getenv("NIXE_HEALTHZ_PATH") or "/healthz"
NIXE_HEALTHZ_SILENCE = _to_int(os.getenv("NIXE_HEALTHZ_SILENCE") or 1, 1)

__all__ = [
    "LOG_CHANNEL_ID",
    "THREAD_NIXE",
    "THREAD_NIXE_DB",
    "THREAD_IMAGEPHISH",
    "THREAD_IMAGEPHISING",
    "BAN_BRAND_NAME",
    "TESTBAN_CHANNEL_ID",
    "PHASH_INBOX_THREAD",
    "PHASH_INBOX_THREAD_ID",
    "PHASH_WATCH_FIRST_DELAY",
    "PHASH_WATCH_INTERVAL",
    "PHASH_FIRST_DELAY_SECONDS",
    "PHASH_INTERVAL_SECONDS",
    "PHASH_DB_MARKER",
    "PHASH_LOG_SCAN_LIMIT",
    "PHASH_HAMMING_MAX",
    "NIXE_HEALTHZ_PATH",
    "NIXE_HEALTHZ_SILENCE",
]