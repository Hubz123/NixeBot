
# -*- coding: utf-8 -*-
"""
a16_lpg_classify_timeout_shield_overlay
--------------------------------------
Wraps gemini classify for Lucky Pull so caller never receives a TimeoutError.
If the original call exceeds a soft timeout or raises, we return a safe
negative classification quickly.

Env knobs (optional, keep your runtime_env.json format):
- LPG_CLASSIFY_SOFT_TIMEOUT_MS (default: 1900)
- LPG_SHIELD_REASON (default: "slow_provider_fallback")
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

SOFT_TIMEOUT = _ms("LPG_CLASSIFY_SOFT_TIMEOUT_MS", 1900)  # ~1.9s
REASON = os.getenv("LPG_SHIELD_REASON", "slow_provider_fallback")

class _TimeoutShield(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        # Try to import original function
        try:
            import nixe.helpers.gemini_bridge as gb
            self._orig = gb.classify_lucky_pull_bytes
        except Exception as e:
            self._orig = None
            log.warning("[lpg-shield] cannot import gemini_bridge: %r", e)

        if self._orig is None:
            return

        async def safe_classify(*args, **kwargs):
            """Call original with a soft timeout; never raise timeout upstream."""
            try:
                return await asyncio.wait_for(self._orig(*args, **kwargs), timeout=SOFT_TIMEOUT)
            except asyncio.TimeoutError:
                # Always return a non-lucky result instead of raising
                return (False, 0.0, "gemini:timeout-shield", REASON)
            except Exception as e:
                # Never bubble up provider/network errors
                return (False, 0.0, "gemini:timeout-shield", f"shield_error({type(e).__name__})")

        # monkeypatch in module so all import sites see shielded version
        try:
            import nixe.helpers.gemini_bridge as gb2
            gb2.classify_lucky_pull_bytes = safe_classify  # type: ignore
            log.warning("[lpg-shield] enabled (soft=%.3fs)", SOFT_TIMEOUT)
        except Exception as e:
            log.warning("[lpg-shield] patch inject failed: %r", e)

async def setup(bot):
    await bot.add_cog(_TimeoutShield(bot))
