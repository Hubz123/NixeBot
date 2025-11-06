
# -*- coding: utf-8 -*-
"""
lpg_cache_memory
----------------
In-memory cache for Lucky Pull classification with persistence via Discord thread.
This module is pure-Python (no Discord import) and can be shared by multiple cogs.

Entry schema:
{
  "sha1": str,     # hex
  "ahash": str,    # 16 hex chars (64-bit)
  "ok": bool,
  "score": float,
  "via": str,
  "reason": str,
  "w": int, "h": int,
  "ts": float      # epoch
}
"""
from __future__ import annotations
import time, io, math, hashlib
from typing import Dict, Optional, Tuple, List

# ---- simple image hashing (aHash 8x8) ----
def _to_ahash_bytes(image_bytes: bytes) -> Tuple[str, Tuple[int,int]]:
    # Lazy PIL import (only if used)
    try:
        from PIL import Image
    except Exception:
        return "0"*16, (0,0)
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("L")
        w, h = im.size
        im = im.resize((8,8))
        px = list(im.getdata())
        avg = sum(px)/len(px) if px else 0
        bits = 0
        for i,v in enumerate(px):
            if v >= avg:
                bits |= (1 << (63-i))
        return f"{bits:016x}", (w,h)
    except Exception:
        return "0"*16, (0,0)

def hamming_hex64(a: str, b: str) -> int:
    try:
        return bin(int(a,16) ^ int(b,16)).count("1")
    except Exception:
        return 64

# ---- memory store ----
_CACHE: Dict[str, dict] = {}  # by sha1
_INDEX_AHASH: Dict[str, List[str]] = {}  # ahash -> [sha1,...]
_MAX = 1000

def configure(max_entries: int = 1000):
    global _MAX
    _MAX = max(10, int(max_entries))

def _evict_if_needed():
    if len(_CACHE) <= _MAX:
        return
    # naive eviction: drop oldest 10%
    n = max(1, len(_CACHE)//10)
    victims = sorted(_CACHE.values(), key=lambda x: x.get("ts",0))[:n]
    for v in victims:
        sha1 = v.get("sha1")
        if not sha1: continue
        a = v.get("ahash")
        _CACHE.pop(sha1, None)
        if a and a in _INDEX_AHASH:
            try:
                _INDEX_AHASH[a].remove(sha1)
                if not _INDEX_AHASH[a]:
                    _INDEX_AHASH.pop(a, None)
            except ValueError:
                pass

def put(image_bytes: bytes, ok: bool, score: float, via: str, reason: str) -> dict:
    sha1 = hashlib.sha1(image_bytes).hexdigest()
    a, wh = _to_ahash_bytes(image_bytes)
    entry = {
        "sha1": sha1, "ahash": a,
        "ok": bool(ok), "score": float(score),
        "via": str(via), "reason": str(reason),
        "w": int(wh[0]), "h": int(wh[1]),
        "ts": time.time()
    }
    _CACHE[sha1] = entry
    _INDEX_AHASH.setdefault(a, []).append(sha1)
    _evict_if_needed()
    return entry

def get_exact(image_bytes: bytes) -> Optional[dict]:
    sha1 = hashlib.sha1(image_bytes).hexdigest()
    return _CACHE.get(sha1)

def get_similar(image_bytes: bytes, max_hamming: int = 6) -> Optional[Tuple[dict, int]]:
    # compute incoming ahash and search neighbors by distance
    a, _ = _to_ahash_bytes(image_bytes)
    best = None
    bestd = 65
    # quick exact bucket
    sha_list = _INDEX_AHASH.get(a, [])
    for sha in sha_list:
        ent = _CACHE.get(sha)
        if ent:
            return ent, 0
    # approx scan (bounded)
    # To keep O(N) low, sample up to 200 entries; real caches are much smaller.
    count = 0
    for ent in _CACHE.values():
        d = hamming_hex64(a, ent.get("ahash","0"*16))
        if d < bestd:
            bestd = d
            best = ent
        count += 1
        if count > 200 and bestd <= max_hamming:
            break
    if best and bestd <= max_hamming:
        return best, bestd
    return None
