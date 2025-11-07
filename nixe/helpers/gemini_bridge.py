# nixe/helpers/gemini_bridge.py
from __future__ import annotations
import os, json, inspect, typing as T

# Try multiple function names to match whichever is present
_burst = None
try:
    from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes_burst as _burst
except Exception:
    try:
        from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes as _burst
    except Exception:
        _burst = None

__all__ = ["classify_lucky_pull_bytes"]

def _cfg():
    return dict(
        mode=os.getenv("LPG_BURST_MODE", "sequential").lower(),
        timeout_ms=float(os.getenv("LPG_BURST_TIMEOUT_MS", "3800")),
        stagger_ms=float(os.getenv("LPG_BURST_STAGGER_MS", "400")),
        early=float(os.getenv("LPG_BURST_EARLY_EXIT_SCORE", "0.90")),
        margin=float(os.getenv("LPG_FALLBACK_MARGIN_MS", "1200")),
    )

async def classify_lucky_pull_bytes(image_bytes: bytes) -> T.Tuple[bool, float, str, str]:
    """
    Normalize to (lucky, score, via, reason).
    """
    if _burst is None:
        return False, 0.0, "none", "classifier_missing"

    cfg = _cfg()
    try:
        # Call with extended signature if supported
        try:
            res = _burst(image_bytes, cfg["mode"], cfg["timeout_ms"], cfg["stagger_ms"], cfg["early"], cfg["margin"])
        except TypeError:
            res = _burst(image_bytes)
        if inspect.isawaitable(res):
            res = await res

        # Normalize tuple forms
        if isinstance(res, (list, tuple)):
            if len(res) == 3:
                ok, score, source = res
                return bool(ok), float(score), str(source), "burst"
            elif len(res) >= 4:
                ok, score, _tag, source = res[:4]
                return bool(ok), float(score), str(source), "burst"
        return False, 0.0, "none", "bridge_normalize_failed"
    except Exception as e:
        return False, 0.0, "none", f"bridge_error:{type(e).__name__}"
