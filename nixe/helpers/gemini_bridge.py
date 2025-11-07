# nixe/helpers/gemini_bridge.py
from __future__ import annotations
import os, inspect, logging, typing as T

log = logging.getLogger(__name__)
__all__ = ["classify_lucky_pull_bytes"]

# Try multiple function names so this works across versions/patches
_burst = None
_burst_name = None
try:
    from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes_burst as _burst
    _burst_name = "classify_lucky_pull_bytes_burst"
except Exception:
    try:
        from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes as _burst
        _burst_name = "classify_lucky_pull_bytes"
    except Exception:
        _burst = None

def _cfg():
    return dict(
        force_burst = os.getenv("LPG_BRIDGE_FORCE_BURST","1") == "1",
        allow_quick = os.getenv("LPG_BRIDGE_ALLOW_QUICK_FALLBACK","0") == "1",
    )

async def classify_lucky_pull_bytes(image_bytes: bytes) -> T.Tuple[bool, float, str, str]:
    """
    Normalize to (lucky, score, via, reason). Always calls burst engine.
    """
    cfg = _cfg()
    if _burst is None:
        log.warning("[gemini-bridge] burst not available -> classifier_missing")
        return False, 0.0, "none", "classifier_missing"
    try:
        res = _burst(image_bytes)
        if inspect.isawaitable(res):
            res = await res
        # Normalize shapes
        if isinstance(res, (list, tuple)):
            if len(res) == 3:
                ok, score, via = res
                return bool(ok), float(score), str(via), "burst"
            elif len(res) >= 4:
                ok, score, _tag, via = res[:4]
                return bool(ok), float(score), str(via), "burst"
        log.warning("[gemini-bridge] unexpected burst result shape: %r", res)
        return False, 0.0, "gemini:bridge", "bridge_normalize_failed"
    except Exception as e:
        # Only allow quick-fallback if explicitly permitted
        if cfg["allow_quick"] and not cfg["force_burst"]:
            return False, 0.0, "gemini:quick-fallback", "slow_provider_fallback"
        return False, 0.0, "none", f"bridge_error:{type(e).__name__}"

log.info("[gemini-bridge] active; burst=%s", _burst_name)
