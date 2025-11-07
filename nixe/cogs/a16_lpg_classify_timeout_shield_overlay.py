# -*- coding: utf-8 -*-
"""
LPG classify timeout shield (v3, safe-load) — variant a16
Lihat penjelasan di a00 file.
"""
from __future__ import annotations
import os, asyncio, time, logging
from discord.ext import commands

log = logging.getLogger(__name__)

def _soft_timeout_seconds() -> float:
    try:
        base_ms = int(os.getenv("LPG_CLASSIFY_SOFT_TIMEOUT_MS", "1900"))
    except Exception:
        base_ms = 1900
    try:
        bridge_force = os.getenv("LPG_BRIDGE_FORCE_BURST", "1") == "1"
        shield_enable = os.getenv("LPG_SHIELD_ENABLE", "").strip()
        if bridge_force and shield_enable != "0":
            per_ms = int(os.getenv("LPG_BURST_TIMEOUT_MS", "3800"))
            margin_ms = int(os.getenv("LPG_FALLBACK_MARGIN_MS", "1200"))
            base_ms = max(base_ms, per_ms + margin_ms + 300)
    except Exception:
        pass
    return max(500, base_ms) / 1000.0

class LPGClassifyTimeoutShieldOverlayV2(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._orig = None
        self.soft_timeout = _soft_timeout_seconds()
        self.allow_quick = os.getenv("LPG_BRIDGE_ALLOW_QUICK_FALLBACK", "0") == "1"
        self.enabled = os.getenv("LPG_SHIELD_ENABLE", "").strip() != "0"

    async def cog_load(self):
        if not self.enabled:
            log.warning("[lpg-shield] disabled via env (LPG_SHIELD_ENABLE=0)")
            return
        try:
            from nixe.helpers import gemini_bridge as gb
        except Exception as e:
            log.warning("[lpg-shield] cannot import gemini_bridge: %r (shield idle)", e)
            return
        if not hasattr(gb, "classify_lucky_pull_bytes"):
            log.warning("[lpg-shield] gemini_bridge.classify_lucky_pull_bytes missing (shield idle)")
            return

        if self._orig is not None:
            return
        self._orig = gb.classify_lucky_pull_bytes

        async def _shielded(image_bytes: bytes, *args, **kwargs):
            try:
                return await asyncio.wait_for(
                    self._orig(image_bytes, *args, **kwargs),
                    timeout=self.soft_timeout
                )
            except asyncio.TimeoutError:
                if self.allow_quick:
                    return False, 0.0, "gemini:quick-fallback", "slow_provider_fallback"
                return False, 0.0, "none", "shield_timeout"
            except Exception as e:
                return False, 0.0, "none", f"shield_error:{type(e).__name__}"

        gb.classify_lucky_pull_bytes = _shielded
        log.info("[lpg-shield/v2] installed; soft_timeout=%.2fs allow_quick=%s", self.soft_timeout, self.allow_quick)

    def cog_unload(self):
        try:
            from nixe.helpers import gemini_bridge as gb
            if self._orig is not None and hasattr(gb, "classify_lucky_pull_bytes"):
                gb.classify_lucky_pull_bytes = self._orig
                log.info("[lpg-shield/v2] restored original classify")
        except Exception:
            pass
        self._orig = None

async def setup(bot: commands.Bot):
    await bot.add_cog(LPGClassifyTimeoutShieldOverlayV2(bot))
