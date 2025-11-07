# -*- coding: utf-8 -*-
# a00_disable_gemini_warmup_overlay.py (no-op patch)
from __future__ import annotations
import logging
from discord.ext import commands

log = logging.getLogger(__name__)

class DisableGeminiWarmup(commands.Cog):
    """No-op: warmup NOT disabled. This patch intentionally leaves warmup running."""
    def __init__(self, bot):
        self.bot = bot
    async def cog_load(self):
        log.warning("[warmup-free:NOOP] overlay disabled; keeping warmup enabled.")
async def setup(bot: commands.Bot):
    await bot.add_cog(DisableGeminiWarmup(bot))
