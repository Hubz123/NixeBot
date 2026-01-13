# -*- coding: utf-8 -*-
"""
LPG classify timeout shield (v4, safe-load, sequential-aware)
- Import lengkap; selalu ada Cog + async setup(bot); tidak pernah FAIL load
- Patching aman; jika import bridge gagal -> overlay idle (tanpa patch)
- SOFT_TIMEOUT dinamis dan sadar mode burst:
    sequential: 2*per_ms - margin_ms + 800  (cap 9500ms)
    stagger:    per_ms + stagger_ms + 800    (cap 9500ms)
    parallel:   per_ms + 700                 (cap 9500ms)
- Tetap bisa dimatikan total: LPG_SHIELD_ENABLE=0
Env:
- LPG_CLASSIFY_SOFT_TIMEOUT_MS (default 1900)
- LPG_BURST_MODE (sequential|stagger|parallel) default sequential
- LPG_BURST_TIMEOUT_MS (default 3800)
- LPG_FALLBACK_MARGIN_MS (default 1200)
- LPG_BURST_STAGGER_MS (default 400)
- LPG_BRIDGE_FORCE_BURST (default 1)
- LPG_BRIDGE_ALLOW_QUICK_FALLBACK (default 0)
- LPG_SHIELD_ENABLE (set 0 untuk mematikan overlay)
"""
from __future__ import annotations
import os, asyncio, logging
from discord.ext import commands

log = logging.getLogger(__name__)

def _soft_timeout_seconds() -> float:
    def geti(name, d):
        try: return int(os.getenv(name, str(d)))
        except Exception: return d
    base_ms = geti("LPG_CLASSIFY_SOFT_TIMEOUT_MS", 1900)
    mode = os.getenv("LPG_BURST_MODE", "sequential").lower()
    per_ms = geti("LPG_BURST_TIMEOUT_MS", 3800)
    margin_ms = geti("LPG_FALLBACK_MARGIN_MS", 1200)
    stagger_ms = geti("LPG_BURST_STAGGER_MS", 400)
    force = os.getenv("LPG_BRIDGE_FORCE_BURST", "1") == "1"
    shield_on = os.getenv("LPG_SHIELD_ENABLE", "").strip() != "0"

    if force and shield_on:
        if mode == "sequential":
            total = (2*per_ms - margin_ms) + 800
        elif mode == "stagger":
            total = per_ms + stagger_ms + 800
        else:  # parallel
            total = per_ms + 700
        base_ms = max(base_ms, min(total, 9500))  # jangan lewati guard 10s
    return max(500, base_ms) / 1000.0

class LPGClassifyTimeoutShieldOverlay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._orig = None
        self.soft_timeout = _soft_timeout_seconds()
        self.allow_quick = os.getenv("LPG_BRIDGE_ALLOW_QUICK_FALLBACK", "0") == "1"
        self.use_burst_fallback = os.getenv('LPG_SHIELD_USE_BURST_FALLBACK','0') == '1'
        try:
            self.burst_fallback_ms = int(os.getenv('LPG_SHIELD_BURST_FALLBACK_MS','1800'))
        except Exception:
            self.burst_fallback_ms = 1800
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
            """Timeout shield for LPG classify.

            Policy: Groq-only. Burst fallback (Gemini) is disabled.
            """
            try:
                return await asyncio.wait_for(
                    self._orig(image_bytes, *args, **kwargs),
                    timeout=self.soft_timeout
                )
            except asyncio.TimeoutError:
                # Do not attempt burst fallback; treat as NOT-LUCKY on soft-timeout
                if self.allow_quick:
                    return (False, 0.0, "timeout", "slow_provider_fallback")
                return (False, 0.0, "timeout", "soft_timeout")
            except Exception as e:
                return (False, 0.0, "none", f"shield_error:{type(e).__name__}")

        gb.classify_lucky_pull_bytes = _shielded
        log.info("[lpg-shield] installed; soft_timeout=%.2fs allow_quick=%s use_burst_fallback=%s", self.soft_timeout, self.allow_quick, self.use_burst_fallback)

    def cog_unload(self):
        # Allow pinning so hot-reload won't restore original classify
        if os.getenv('LPG_SHIELD_PIN','0') == '1':
            try:
                import logging as _logging
                _logging.getLogger(__name__).warning('[lpg-shield] pin=1 -> skip restore on cog_unload')
            except Exception:
                pass
            self._orig = None
            return
        try:
            from nixe.helpers import gemini_bridge as gb
            if self._orig is not None and hasattr(gb, "classify_lucky_pull_bytes"):
                gb.classify_lucky_pull_bytes = self._orig
                log.info("[lpg-shield] restored original classify")
        except Exception:
            pass
        self._orig = None

async def setup(bot: commands.Bot):
    await bot.add_cog(LPGClassifyTimeoutShieldOverlay(bot))
