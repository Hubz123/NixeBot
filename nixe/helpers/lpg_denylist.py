# -*- coding: utf-8 -*-
"""
lpg_denylist
------------
Persistent "banish" list for LPG false positives.

Goal:
- If a user deletes a LUCKY memory entry from the memory thread, we treat it as an
  explicit false-positive signal and permanently deny that image (and near-duplicates)
  from being classified/stored as LUCKY again.

Persistence:
- Prefer a Discord thread specified by:
    LPG_DENYLIST_THREAD_ID, else LPG_WHITELIST_THREAD_ID
  Each deny entry is posted as a single line message:
    deny sha1=<hex40> ahash=<hex16>
- If no thread id is available, denylist is in-RAM only.

Matching:
- Exact SHA1 match => denied
- aHash near-duplicate match if hamming <= LPG_DENYLIST_AHASH_MAXD (default 6)
"""

from __future__ import annotations

import os, re, time, logging
from typing import Optional, Tuple, Dict, Set, List

log = logging.getLogger("nixe.helpers.lpg_denylist")

_SHA: Set[str] = set()
_AHASH: Dict[str, Set[str]] = {}  # ahash -> {sha1,...}
_LAST_LOAD_TS: float = 0.0

_RX = re.compile(r"sha1=([0-9a-f]{40}).*?ahash=([0-9a-f]{16})", re.IGNORECASE)

def _env_int(k: str, default: int) -> int:
    try:
        return int(os.getenv(k, str(default)))
    except Exception:
        return default

def hamming_hex64(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 64

def thread_id() -> int:
    for k in ("LPG_DENYLIST_THREAD_ID", "LPG_WHITELIST_THREAD_ID"):
        v = (os.getenv(k) or "").strip()
        if v.isdigit():
            return int(v)
    return 0

def add(sha1: str, ahash: str) -> None:
    sha1 = (sha1 or "").strip().lower()
    ahash = (ahash or "").strip().lower()
    if not sha1 or len(sha1) != 40:
        return
    if not ahash or len(ahash) != 16:
        ahash = "0" * 16
    if sha1 in _SHA:
        return
    _SHA.add(sha1)
    s = _AHASH.get(ahash)
    if s is None:
        s = set()
        _AHASH[ahash] = s
    s.add(sha1)

def is_denied(sha1: str, ahash: str) -> Tuple[bool, str]:
    """Return (denied, reason)."""
    sha1 = (sha1 or "").strip().lower()
    ahash = (ahash or "").strip().lower()
    if sha1 and sha1 in _SHA:
        return True, "deny_sha1"
    if not ahash or len(ahash) != 16:
        return False, "no_ahash"
    # exact ahash bucket
    if ahash in _AHASH and _AHASH[ahash]:
        return True, "deny_ahash_exact"
    maxd = _env_int("LPG_DENYLIST_AHASH_MAXD", 6)
    if maxd < 0:
        return False, "deny_near_off"
    # near-scan ahash keys (bounded)
    best = 65
    for k in list(_AHASH.keys()):
        d = hamming_hex64(ahash, k)
        if d < best:
            best = d
            if best <= maxd:
                return True, f"deny_ahash_near(d={best})"
    return False, "ok"

async def load_from_thread(bot) -> int:
    """Scan denylist thread and populate in-RAM denylist. Returns count loaded."""
    global _LAST_LOAD_TS
    tid = thread_id()
    if not tid:
        return 0
    try:
        ch = bot.get_channel(tid) or await bot.fetch_channel(tid)
    except Exception as e:
        log.warning("[denylist] fetch_channel failed tid=%s: %r", tid, e)
        return 0
    if ch is None:
        return 0
    loaded = 0
    # bounded scan (oldest->newest). Keep it conservative on Render.
    limit = _env_int("LPG_DENYLIST_BOOT_SCAN_LIMIT", 2000)
    try:
        async for msg in ch.history(limit=limit, oldest_first=True):
            if not msg:
                continue
            content = (getattr(msg, "content", "") or "").strip()
            m = _RX.search(content)
            if not m:
                # also allow footer text from embeds
                try:
                    for emb in (msg.embeds or []):
                        ft = (getattr(getattr(emb, "footer", None), "text", "") or "")
                        mm = _RX.search(ft)
                        if mm:
                            m = mm
                            break
                except Exception:
                    pass
            if m:
                add(m.group(1), m.group(2))
                loaded += 1
    except Exception as e:
        log.warning("[denylist] history scan failed: %r", e)
    _LAST_LOAD_TS = time.time()
    return loaded

async def persist_to_thread(bot, sha1: str, ahash: str) -> bool:
    """Append deny entry to denylist thread. Returns True if sent."""
    tid = thread_id()
    if not tid:
        return False
    try:
        ch = bot.get_channel(tid) or await bot.fetch_channel(tid)
        if not ch:
            return False
        line = f"deny sha1={sha1} ahash={ahash}"
        await ch.send(content=line)
        return True
    except Exception as e:
        log.warning("[denylist] persist failed: %r", e)
        return False
