
# -*- coding: utf-8 -*-
"""
a16_lpg_gemini_burst_overlay
----------------------------
Monkeypatch Lucky Pull classification to use dual-Gemini BURST with strict
deadline (no timeouts propagate). Keeps original return signature.
"""

from __future__ import annotations
import os
import logging
import asyncio
from discord.ext import commands

log = logging.getLogger(__name__)

class _BurstPatch(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Import our burst helper
        try:
            from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes_burst as _burst
            self._burst = _burst
        except Exception as e:
            self._burst = None
            log.warning("[lpg-burst] import failed: %r", e)
            return

        # Install monkeypatch
        try:
            import nixe.helpers.gemini_bridge as gb
            async def patched(img_bytes: bytes, *args, **kwargs):
                return await self._burst(img_bytes)
            gb.classify_lucky_pull_bytes = patched  # type: ignore
            log.warning("[lpg-burst] enabled. keys=%s", os.getenv("GEMINI_API_KEYS","[env]"))
        except Exception as e:
            log.warning("[lpg-burst] patch inject failed: %r", e)

async def setup(bot):
    await bot.add_cog(_BurstPatch(bot))
