# -*- coding: utf-8 -*-
"""
lpg_cache_memory
----------------
In-memory cache for Lucky Pull classification.

Design goals:
- No long-lived image bytes in RAM (store only fingerprints + metadata).
- Support bounded caches (for low-RAM hosts such as Render Free) and optionally unbounded
  caches (for minipc), with periodic maintenance handled by the persistence overlay.

Entry schema:
{
  "sha1": str,     # hex (exact content)
  "ahash": str,    # 16 hex chars (64-bit aHash)
  "ok": bool,
  "score": float,
  "via": str,
  "reason": str,
  "w": int, "h": int,
  "ts": float      # epoch seconds
}
"""

from __future__ import annotations

import hashlib
import io
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple


# -----------------------------
# Simple image hashing (aHash 8x8)
# -----------------------------
def _to_ahash_bytes(image_bytes: bytes) -> Tuple[str, Tuple[int, int]]:
    """Return (ahash_hex64, (w,h)). Best-effort: returns zeros if PIL unavailable."""
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return "0" * 16, (0, 0)

    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("L")
        w, h = im.size
        im = im.resize((8, 8))
        px = list(im.getdata())
        avg = (sum(px) / len(px)) if px else 0
        bits = 0
        for i, v in enumerate(px):
            if v >= avg:
                bits |= (1 << (63 - i))
        try:
            im.close()
        except Exception:
            pass
        return f"{bits:016x}", (w, h)
    except Exception:
        return "0" * 16, (0, 0)


def hamming_hex64(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 64


# -----------------------------
# Memory store
# -----------------------------
_CACHE: Dict[str, dict] = {}  # sha1 -> entry
_INDEX_AHASH: Dict[str, List[str]] = {}  # ahash -> [sha1, ...]

# msg_id -> sha1 mapping (for delete=unlearn without needing embed/footer)
# Keep bounded to avoid leaks on long-running instances.
_MSGID_TO_SHA1 = OrderedDict()  # type: ignore[var-annotated]

def _msgid_cap() -> int:
    # Tie cap loosely to cache size; keep a sensible upper bound.
    try:
        if _MAX and int(_MAX) > 0:
            return max(5000, int(_MAX) * 2)
    except Exception:
        pass
    return 50000

def register_msgid_sha1(msg_id: int, sha1: str) -> None:
    try:
        mid = int(msg_id or 0)
        if mid <= 0:
            return
        s = str(sha1 or "").strip()
        if not s:
            return
        _MSGID_TO_SHA1[mid] = s
        try:
            _MSGID_TO_SHA1.move_to_end(mid)
        except Exception:
            pass
        cap = _msgid_cap()
        if len(_MSGID_TO_SHA1) > cap:
            # evict oldest 10%
            drop = max(1, int(cap * 0.10))
            for _ in range(drop):
                try:
                    _MSGID_TO_SHA1.popitem(last=False)
                except Exception:
                    break
    except Exception:
        return

def pop_msgid_sha1(msg_id: int) -> Optional[str]:
    try:
        mid = int(msg_id or 0)
        if mid <= 0:
            return None
        return _MSGID_TO_SHA1.pop(mid, None)
    except Exception:
        return None

# _MAX:
#   - if >0 : bounded cache (evict oldest 10% when exceeded)
#   - if <=0: unbounded (minipc mode); caller must do periodic maintenance
_MAX = 1000


def configure(max_entries: int = 1000) -> None:
    """Configure cache size. max_entries<=0 disables eviction (unbounded)."""
    global _MAX
    try:
        m = int(max_entries)
    except Exception:
        m = 1000
    _MAX = m  # allow <=0 for unbounded


def stats() -> dict:
    return {"entries": len(_CACHE), "buckets": len(_INDEX_AHASH), "max": _MAX}


def _index_add(sha1: str, ah: str) -> None:
    _INDEX_AHASH.setdefault(ah, []).append(sha1)


def _index_remove(sha1: str, ah: str) -> None:
    lst = _INDEX_AHASH.get(ah)
    if not lst:
        return
    try:
        lst.remove(sha1)
    except ValueError:
        return
    if not lst:
        _INDEX_AHASH.pop(ah, None)


def _evict_if_needed() -> None:
    if _MAX <= 0:
        return
    if len(_CACHE) <= _MAX:
        return
    # naive eviction: drop oldest 10%
    n = max(1, len(_CACHE) // 10)
    victims = sorted(_CACHE.values(), key=lambda x: float(x.get("ts", 0.0)))[:n]
    for v in victims:
        sha1 = str(v.get("sha1") or "")
        if not sha1:
            continue
        ah = str(v.get("ahash") or "")
        _CACHE.pop(sha1, None)
        if ah:
            _index_remove(sha1, ah)


def upsert_entry(entry: dict) -> None:
    """Insert an already-computed entry (sha1/ahash must exist)."""
    sha1 = str(entry.get("sha1") or "")
    ah = str(entry.get("ahash") or "0" * 16)
    if not sha1:
        return
    # If replacing, remove old index first
    old = _CACHE.get(sha1)
    if old:
        try:
            _index_remove(sha1, str(old.get("ahash") or ""))
        except Exception:
            pass
    _CACHE[sha1] = entry
    _index_add(sha1, ah)
    _evict_if_needed()


def remove_sha1(sha1: str) -> None:
    """Remove an entry by sha1 (used for delete=unlearn)."""
    sha1 = str(sha1 or "")
    if not sha1:
        return
    old = _CACHE.pop(sha1, None)
    if not old:
        return
    ah = str(old.get("ahash") or "")
    if ah:
        _index_remove(sha1, ah)


def put(image_bytes: bytes, ok: bool, score: float, via: str, reason: str) -> dict:
    sha1 = hashlib.sha1(image_bytes).hexdigest()
    ah, wh = _to_ahash_bytes(image_bytes)
    entry = {
        "sha1": sha1,
        "ahash": ah,
        "ok": bool(ok),
        "score": float(score),
        "via": str(via),
        "reason": str(reason),
        "w": int(wh[0]),
        "h": int(wh[1]),
        "ts": time.time(),
    }
    upsert_entry(entry)
    return entry


def get_exact(image_bytes: bytes) -> Optional[dict]:
    sha1 = hashlib.sha1(image_bytes).hexdigest()
    return _CACHE.get(sha1)


def get_similar(image_bytes: bytes, max_hamming: int = 6) -> Optional[Tuple[dict, int]]:
    ah, _ = _to_ahash_bytes(image_bytes)
    # exact bucket
    sha_list = _INDEX_AHASH.get(ah, [])
    for sha in sha_list:
        ent = _CACHE.get(sha)
        if ent:
            return ent, 0

    # bounded approximate scan for safety
    best = None
    bestd = 65
    count = 0
    for ent in _CACHE.values():
        d = hamming_hex64(ah, str(ent.get("ahash") or ("0" * 16)))
        if d < bestd:
            bestd = d
            best = ent
        count += 1
        # On huge caches, stop early once a good-enough match appears.
        if count > 500 and bestd <= max_hamming:
            break

    if best and bestd <= max_hamming:
        return best, bestd
    return None
