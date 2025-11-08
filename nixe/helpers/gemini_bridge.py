# nixe/helpers/gemini_bridge.py
from __future__ import annotations
import os, inspect, logging, typing as T

log = logging.getLogger(__name__)
__all__ = ["classify_lucky_pull_bytes"]

# Optional provider bridge (direct Gemini/Groq etc.)
_provider = None
try:
    # sync function expected: classify_with_image_bytes(image_bytes) -> (prob: float, via: str)
    from nixe.helpers.lpa_provider_bridge import classify_with_image_bytes as _provider  # type: ignore
except Exception:
    _provider = None

# Burst engine (fast + timeout)
_burst = None
_burst_name = None
try:
    # prefer newer name if present
    from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes_burst as _burst  # type: ignore
    _burst_name = "classify_lucky_pull_bytes_burst"
except Exception:
    try:
        from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes as _burst  # type: ignore
        _burst_name = "classify_lucky_pull_bytes"
    except Exception:
        _burst = None

def _cfg():
    return dict(
        force_burst = os.getenv("LPG_BRIDGE_FORCE_BURST","1") == "1",
        allow_quick = os.getenv("LPG_BRIDGE_ALLOW_QUICK_FALLBACK","0") == "1",
        thr        = float(os.getenv("GEMINI_LUCKY_THRESHOLD","0.85")),
    )

def _norm_prob(x: T.Any) -> float:
    try:
        v = float(x)
        if v < 0: return 0.0
        if v > 1: return 1.0
        return v
    except Exception:
        return 0.0

async def classify_lucky_pull_bytes(image_bytes: bytes) -> T.Tuple[bool, float, str, str]:
    """Normalize to (lucky, score, via, reason).

    Behavior:
      - If LPG_BRIDGE_FORCE_BURST=0 and provider bridge exists, try provider FIRST (direct Gemini).
      - Otherwise call burst engine.
      - On burst error and LPG_BRIDGE_ALLOW_QUICK_FALLBACK=1, try provider as a quick fallback.
    """
    cfg = _cfg()

    # Provider-first path (makes main.py behavior match SMOKE path)
    if not cfg["force_burst"] and _provider is not None:
        try:
            prob, via = _provider(image_bytes)  # expected: (float prob, str via)
            score = _norm_prob(prob)
            lucky = bool(score >= cfg["thr"])
            return lucky, score, str(via or "provider"), "provider"
        except Exception as e:
            log.warning("[gemini-bridge] provider-first failed: %r; falling back to burst", e)

    # Burst path (default)
    if _burst is None:
        return False, 0.0, "none", "burst_unavailable"

    try:
        # burst result shapes seen in codebase:
        #  - (ok, score, tag, via, *flags)
        #  - (ok, score, via)
        res = await _burst(image_bytes)  # type: ignore
        if isinstance(res, tuple):
            if len(res) >= 5:
                ok, score, _tag, via, *_ = res
                return bool(ok), float(score), str(via), "burst"
            elif len(res) >= 4:
                ok, score, _tag, via = res[:4]
                return bool(ok), float(score), str(via), "burst"
            elif len(res) >= 3:
                ok, score, via = res
                return bool(ok), float(score), str(via), "burst"
        log.warning("[gemini-bridge] unexpected burst result shape: %r", res)
        return False, 0.0, "gemini:bridge", "bridge_normalize_failed"
    except Exception as e:
        # Only allow quick-fallback if explicitly permitted
        if cfg["allow_quick"] and not cfg["force_burst"] and _provider is not None:
            try:
                prob, via = _provider(image_bytes)
                score = _norm_prob(prob)
                lucky = bool(score >= cfg["thr"])
                return lucky, score, str(via or "provider"), "quick-fallback"
            except Exception:
                pass
        return False, 0.0, "none", f"bridge_error:{type(e).__name__}"

log.info("[gemini-bridge] active; burst=%s, provider=%s", _burst_name, bool(_provider))
