# -*- coding: utf-8 -*-
"""
nixe.cogs.lucky_pull_auto — classifier unpack fix
"""
from __future__ import annotations

import os, logging, asyncio
from typing import Optional, List, Tuple, Any
import discord
from discord.ext import commands

log = logging.getLogger(__name__)

try:
    from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes as classify_bytes
except Exception:
    classify_bytes = None  # type: ignore

def _provider_threshold(provider: str) -> float:
    try: eps = float(os.getenv("LPG_CONF_EPSILON") or 0.0)
    except Exception: eps = 0.0
    if provider and provider.lower().startswith("gemini"):
        try: thr = float(os.getenv("GEMINI_LUCKY_THRESHOLD") or os.getenv("LPG_GEMINI_THRESHOLD") or 0.85)
        except Exception: thr = 0.85
        return max(0.0, min(1.0, thr - eps))
    try: thr = float(os.getenv("LPG_GROQ_THRESHOLD") or 0.50)
    except Exception: thr = 0.50
    return max(0.0, min(1.0, thr - eps))

def _normalize_classifier_result(res: Any) -> Tuple[bool, float, str, str]:
    ok=False; score=0.0; provider="unknown"; reason=""
    if isinstance(res, dict):
        score = float(res.get("score") or res.get("prob") or res.get("p") or 0.0)
        provider = str(res.get("provider") or res.get("via") or "unknown")
        reason = str(res.get("reason") or "")
        if "ok" in res: ok = bool(res.get("ok"))
        else: ok = score >= _provider_threshold(provider)
        return ok, score, provider, reason
    if isinstance(res, (list, tuple)):
        if len(res) >= 4:
            ok, score, provider, reason = res[0], float(res[1]), str(res[2]), str(res[3])
            return bool(ok), float(score), provider, reason
        if len(res) == 3:
            score, provider, reason = float(res[0]), str(res[1]), str(res[2])
            return score >= _provider_threshold(provider), score, provider, reason
        if len(res) == 2:
            score, provider = float(res[0]), str(res[1])
            return score >= _provider_threshold(provider), score, provider, ""
        if len(res) == 1:
            score = float(res[0])
            return score >= _provider_threshold("unknown"), score, "unknown", ""
    try:
        score = float(res)
        return score >= _provider_threshold("unknown"), score, "unknown", ""
    except Exception:
        pass
    return False, 0.0, "unknown", "invalid_result"

class _LPAClassifyMixin:
    timeout_ms: int
    providers: List[str]
    async def _classify(self, img_bytes: bytes, *, text: str = "") -> Tuple[bool, float, str, str]:
        if classify_bytes is None:
            return False, 0.0, "none", "classifier_unavailable"
        loop = asyncio.get_running_loop()
        try:
            def _call():
                return classify_bytes(img_bytes, text=text, timeout_ms=self.timeout_ms, providers=getattr(self, "providers", None))
            res = await loop.run_in_executor(None, _call)
        except Exception as e:
            log.warning("[lpa] classify call failed: %r", e)
            return False, 0.0, "none", "exception"
        try:
            return _normalize_classifier_result(res)
        except Exception as e:
            log.warning("[lpa] normalize failed: %r (res=%r)", e, res)
            return False, 0.0, "none", "normalize_exception"
