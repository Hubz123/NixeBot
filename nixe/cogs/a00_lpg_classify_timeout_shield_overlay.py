
# -*- coding: utf-8 -*-
"""
a00_lpg_classify_timeout_shield_overlay
--------------------------------------
Early overlay that wraps Gemini lucky-pull classification with a soft timeout
so callers never receive a TimeoutError. Works in both local & Render.
Returns a safe, non-lucky verdict on slow/failed calls.

Env (optional):
- LPG_CLASSIFY_SOFT_TIMEOUT_MS (default: 1900)
- LPG_SHIELD_REASON (default: "slow_provider_fallback")
- LPG_SHIELD_TAG (default: "gemini:quick-fallback")
"""
from __future__ import annotations
import os
import asyncio
import logging
from discord.ext import commands

log = logging.getLogger(__name__)

def _ms(name: str, default: int) -> float:
    try:
        return float(os.getenv(name, str(default))) / 1000.0
    except Exception:
        return default / 1000.0

SOFT_TIMEOUT = _ms("LPG_CLASSIFY_SOFT_TIMEOUT_MS", 1900)
REASON = os.getenv("LPG_SHIELD_REASON", "slow_provider_fallback")
TAG = os.getenv("LPG_SHIELD_TAG", "gemini:quick-fallback")

class _EarlyTimeoutShield(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        try:
            import nixe.helpers.gemini_bridge as gb
            self._orig = gb.classify_lucky_pull_bytes
        except Exception as e:
            self._orig = None
            log.warning("[lpg-shield] cannot import gemini_bridge: %r", e)

        if self._orig is None:
            return

        async def safe_classify(*args, **kwargs):
            try:
                return await asyncio.wait_for(self._orig(*args, **kwargs), timeout=SOFT_TIMEOUT)
            except asyncio.TimeoutError:
                return (False, 0.0, TAG, REASON)
            except Exception as e:
                return (False, 0.0, TAG, f"shield_error({type(e).__name__})")

        # Inject monkeypatch
        try:
            import nixe.helpers.gemini_bridge as gb2
            gb2.classify_lucky_pull_bytes = safe_classify  # type: ignore
            log.warning("[lpg-shield] enabled early (soft=%.3fs tag=%s)", SOFT_TIMEOUT, TAG)
        except Exception as e:
            log.warning("[lpg-shield] patch inject failed: %r", e)

async def setup(bot):
    await bot.add_cog(_EarlyTimeoutShield(bot))
