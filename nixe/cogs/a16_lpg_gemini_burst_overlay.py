# -*- coding: utf-8 -*-
"""nixe.cogs.a16_lpg_gemini_burst_overlay

C025 QC policy guard:
- In this project, GEMINI_API_KEY / GEMINI_API_KEY_B are Groq keys reserved for LPG.
- Google Gemini (TRANSLATE_GEMINI_API_KEY) is translate-only.
Therefore, any LPG path that calls Google Gemini REST is disallowed.

This overlay is intentionally disabled permanently to prevent accidental
Google-Gemini calls outside translate.
"""

from __future__ import annotations

import logging
from discord.ext import commands

log = logging.getLogger(__name__)


class LPGGeminiBurstOverlayDisabled(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        log.info("[lpg-burst] disabled permanently (policy: Google Gemini is translate-only)")


async def setup(bot: commands.Bot):
    await bot.add_cog(LPGGeminiBurstOverlayDisabled(bot))
