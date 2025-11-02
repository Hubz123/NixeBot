# -*- coding: utf-8 -*-
"""
a16_lpa_classifier_unpack_overlay_v2
------------------------------------
Hotfix yang *tidak mengubah* config/format lain:
- Monkey‑patch LuckyPullAuto._classify() supaya SELALU mengembalikan (prob, via) dua‑tuple.
- Menerima output bridge (4/3/2 tuple, dict, float) dan menormalkan.
- Menerima argumen `text` **sebagai positional maupun keyword** (def _patched_classify(self, img_bytes, text=None)).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Tuple

log = logging.getLogger(__name__)

def _to_prob_via(res: Any) -> Tuple[float, str]:
    # dict
    if isinstance(res, dict):
        try: prob = float(res.get("score") or res.get("prob") or res.get("p") or 0.0)
        except Exception: prob = 0.0
        via  = str(res.get("provider") or res.get("via") or "unknown")
        return prob, via
    # tuple/list
    if isinstance(res, (list, tuple)):
        n = len(res)
        if n >= 4:
            # (ok, score, provider, reason)
            try: return float(res[1]), str(res[2])
            except Exception: return 0.0, "invalid_result"
        if n == 3:
            # (score, provider, reason)
            try: return float(res[0]), str(res[1])
            except Exception: return 0.0, "invalid_result"
        if n == 2:
            # (score, provider)
            try: return float(res[0]), str(res[1])
            except Exception: return 0.0, "invalid_result"
        if n == 1:
            # (score,)
            try: return float(res[0]), "unknown"
            except Exception: return 0.0, "invalid_result"
    # single numeric
    try:
        return float(res), "unknown"
    except Exception:
        pass
    return 0.0, "invalid_result"

async def setup(bot):
    try:
        from nixe.cogs import lucky_pull_auto as _lpa
    except Exception as e:
        log.warning("[lpa-unpack-v2] lucky_pull_auto not available: %r", e)
        return

    try:
        from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes as _bridge
    except Exception as e:
        _bridge = None
        log.warning("[lpa-unpack-v2] gemini bridge not available: %r", e)

    if not hasattr(_lpa, "LuckyPullAuto"):
        log.warning("[lpa-unpack-v2] LuckyPullAuto class not found; no patch applied")
        return

    async def _patched_classify(self, img_bytes, text=None):
        """Return exactly (prob, via) to match legacy call sites (supports positional `text`)."""
        if _bridge is None:
            return 0.0, "classifier_unavailable"
        loop = asyncio.get_running_loop()
        try:
            def _call():
                timeout_ms = getattr(self, "timeout_ms", 20000)
                providers = getattr(self, "providers", None)
                return _bridge(img_bytes, text=text or "", timeout_ms=timeout_ms, providers=providers)
            res = await loop.run_in_executor(None, _call)
        except Exception as e:
            log.warning("[lpa-unpack-v2] bridge call failed: %r", e)
            return 0.0, "exception"
        try:
            prob, via = _to_prob_via(res)
            return float(prob), str(via)
        except Exception as e:
            log.warning("[lpa-unpack-v2] normalize failed: %r (res=%r)", e, res)
            return 0.0, "normalize_exception"

    try:
        _lpa.LuckyPullAuto._classify = _patched_classify  # type: ignore[attr-defined]
        log.warning("[lpa-unpack-v2] Applied 2-tuple normalize patch (positional text supported)")
    except Exception as e:
        log.warning("[lpa-unpack-v2] Failed to patch LuckyPullAuto._classify: %r", e)
