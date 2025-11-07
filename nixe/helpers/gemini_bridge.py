# nixe/helpers/gemini_bridge.py
from __future__ import annotations
import os, inspect, logging, typing as T

log = logging.getLogger(__name__)

# Try multiple function names so this works across versions/patches
_burst = None
try:
    from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes_burst as _burst
    _burst_name = "classify_lucky_pull_bytes_burst"
except Exception:
    try:
        from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes as _burst
        _burst_name = "classify_lucky_pull_bytes"
    except Exception:
        _burst = None
        _burst_name = None

__all__ = ["classify_lucky_pull_bytes"]

def _cfg():
    return dict(
        mode=os.getenv("LPG_BURST_MODE", "sequential").lower(),
        timeout_ms=float(os.getenv("LPG_BURST_TIMEOUT_MS", "3800")),
        stagger_ms=float(os.getenv("LPG_BURST_STAGGER_MS", "400")),
        early=float(os.getenv("LPG_BURST_EARLY_EXIT_SCORE", "0.90")),
        margin=float(os.getenv("LPG_FALLBACK_MARGIN_MS", "1200")),
        allow_quick=os.getenv("LPG_BRIDGE_ALLOW_QUICK_FALLBACK","0")=="1",
        force_burst=os.getenv("LPG_BRIDGE_FORCE_BURST","1")=="1",
    )

async def classify_lucky_pull_bytes(image_bytes: bytes) -> T.Tuple[bool, float, str, str]:
    """
    Normalize to (lucky, score, via, reason).
    This bridge *forces* routing to the BURST engine (sequential/stagger/parallel)
    so that Render Free timeouts use the same tuned budget. We only fall back to
    any internal 'quick-fallback' path if LPG_BRIDGE_ALLOW_QUICK_FALLBACK=1.
    """
    cfg = _cfg()
    if _burst is None:
        log.warning("[gemini-bridge] burst not available -> classifier_missing")
        return False, 0.0, "none", "classifier_missing"

    # Call burst (prefer simple signature; env carries the knobs)
    try:
        res = _burst(image_bytes)
        if inspect.isawaitable(res):
            res = await res

        # Common shapes:
        # (ok, score, "gemini:api1-early") OR (ok, score, tag, "api1-early")
        if isinstance(res, (list, tuple)):
            if len(res) == 3:
                ok, score, source = res
                via = source if isinstance(source, str) else "gemini"
                return bool(ok), float(score), str(via), "burst"
            elif len(res) >= 4:
                ok, score, _tag, source = res[:4]
                via = source if isinstance(source, str) else "gemini"
                return bool(ok), float(score), str(via), "burst"

        # If we reached here, normalize failed; be conservative
        log.warning("[gemini-bridge] unexpected burst result shape: %r", res)
        return False, 0.0, "gemini:bridge", "bridge_normalize_failed"
    except Exception as e:
        # Only allow quick-fallback if explicitly permitted
        if cfg["allow_quick"] and not cfg["force_burst"]:
            log.warning("[gemini-bridge] burst error -> quick-fallback allowed: %s", type(e).__name__)
            return False, 0.0, "gemini:quick-fallback", "slow_provider_fallback"
        log.error("[gemini-bridge] burst error (no quick-fallback): %s", type(e).__name__)
        return False, 0.0, "none", f"bridge_error:{type(e).__name__}"

# Log on import so we can verify which bridge is active on Render
log.info("[gemini-bridge] active; burst=%s force_burst=%s allow_quick=%s",
         _burst_name, _cfg()["force_burst"], _cfg()["allow_quick"])
