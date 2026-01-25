import hashlib, time, os, logging
from typing import Dict, Tuple, Optional, List
try:
    from PIL import Image
    import io
    _PIL = True
except Exception:
    _PIL = False

_CACHE: Dict[str, Tuple[float, float, str]] = {}  # key -> (ts, score, provider)

def clear() -> None:
    """Drop all cached entries (best-effort)."""
    try:
        _CACHE.clear()
    except Exception:
        pass

def prune_to(limit: int) -> None:
    """Prune cache to at most `limit` entries by dropping oldest."""
    try:
        lim = int(limit or 0)
        if lim <= 0:
            return
        if len(_CACHE) <= lim:
            return
        items = sorted(_CACHE.items(), key=lambda kv: kv[1][0])
        for kk, _ in items[: max(0, len(_CACHE) - lim)]:
            _CACHE.pop(kk, None)
    except Exception:
        pass

def _ttl_seconds() -> int:
    return int(os.getenv("LPG_CACHE_TTL_SEC","86400"))  # 24h default

def _limit() -> int:
    return int(os.getenv("LPG_CACHE_MAX_ENTRIES","5000"))

def _hash_sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()

def _phash(b: bytes) -> Optional[str]:
    if not _PIL: return None
    try:
        from imagehash import phash
        im = Image.open(io.BytesIO(b)).convert("RGB")
        return str(phash(im))
    except Exception:
        return None

def key_for_image(b: bytes) -> str:
    h = _phash(b)
    if h: return f"p:{h}"
    return f"s:{_hash_sha1(b)}"

def get(b: bytes) -> Optional[Tuple[float, str]]:
    k = key_for_image(b)
    ent = _CACHE.get(k)
    if not ent: return None
    ts, score, provider = ent
    if ts + _ttl_seconds() < time.time():
        try: del _CACHE[k]
        except Exception: pass
        return None
    return (score, provider)

def put(b: bytes, score: float, provider: str):
    # gc
    if len(_CACHE) > _limit():
        # drop oldest ~10%
        items = sorted(_CACHE.items(), key=lambda kv: kv[1][0])
        for i,(kk,_) in enumerate(items[:max(10, int(0.1*len(items)))]):
            _CACHE.pop(kk, None)
    k = key_for_image(b)
    _CACHE[k] = (time.time(), float(score), str(provider))

def debug_snapshot(max_items:int=50) -> List[str]:
    now = time.time()
    rows = []
    for k,(ts,score,prov) in sorted(_CACHE.items(), key=lambda kv: kv[1][0], reverse=True)[:max_items]:
        age = int(now - ts)
        rows.append(f"{k[:18]}..  score={score:.2f}  {prov}  age={age}s")
    return rows
