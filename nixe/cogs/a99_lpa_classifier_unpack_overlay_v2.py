
import logging, inspect, asyncio
from discord.ext import commands

async def setup(bot):
    log = logging.getLogger(__name__)
    try:
        from nixe.cogs import lucky_pull_auto as _lpa
    except Exception as e:
        log.warning("[lpa-unpack-v2] setup: cannot import lucky_pull_auto: %r", e)
        return

    # Resolve classifier function with fallback chain
    _cls = None
    try:
        from nixe.helpers.lpa_provider_bridge import classify_with_image_bytes as _cls  # sync
        log.debug("[lpa-unpack-v2] using lpa_provider_bridge.classify_with_image_bytes")
    except Exception as e1:
        log.warning("[lpa-unpack-v2] provider bridge import failed: %r; trying gemini_bridge...", e1)
        try:
            from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes as _cls  # async
            log.debug("[lpa-unpack-v2] fallback to gemini_bridge.classify_lucky_pull_bytes")
        except Exception as e2:
            log.warning("[lpa-unpack-v2] gemini bridge not available: %r", e2)
            _cls = None

    def _to_prob_via(res):
        try:
            if isinstance(res, tuple):
                if len(res) >= 2 and isinstance(res[1], str):
                    p = float(res[0]) if isinstance(res[0], (int, float)) else 0.0
                    return max(0.0, min(1.0, p)), res[1]
                if len(res) >= 1:
                    p = float(res[0]) if isinstance(res[0], (int, float)) else 0.0
                    return max(0.0, min(1.0, p)), "gemini"
            if isinstance(res, dict):
                p = float(res.get("score", 0.0))
                via = str(res.get("provider") or res.get("via") or "gemini")
                return max(0.0, min(1.0, p)), via
            if isinstance(res, (int, float)):
                p = float(res)
                return max(0.0, min(1.0, p)), "gemini"
        except Exception:
            pass
        return 0.0, "invalid"

    async def _patched_classify(self, img_bytes, text=None):
        if _cls is None:
            return 0.0, "classifier_unavailable"
        try:
            res = _cls(img_bytes)
            if inspect.isawaitable(res):
                timeout_ms = getattr(self, "timeout_ms", 20000)
                try:
                    res = await asyncio.wait_for(res, timeout_ms/1000.0)
                except asyncio.TimeoutError:
                    return 0.0, "timeout"
        except Exception as e:
            log.warning("[lpa-unpack-v2] bridge call failed: %r", e)
            return 0.0, "exception"
        try:
            return _to_prob_via(res)
        except Exception as e:
            log.warning("[lpa-unpack-v2] normalize failed: %r (res=%r)", e, res)
            return 0.0, "normalize_exception"

    try:
        _lpa.LuckyPullAuto._classify = _patched_classify  # type: ignore[attr-defined]
        log.warning("[lpa-unpack-v2] Applied 2-tuple normalize patch (positional text supported)")
    except Exception as e:
        log.warning("[lpa-unpack-v2] Failed to patch LuckyPullAuto._classify: %r", e)
