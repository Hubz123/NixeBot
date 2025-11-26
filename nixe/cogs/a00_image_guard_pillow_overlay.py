from __future__ import annotations

import logging

from discord.ext import commands

from nixe.utils import image_guard

log = logging.getLogger(__name__)


class PillowImageGuardOverlay(commands.Cog):
    """Overlay that applies global Pillow image safety limits at startup.

    This runs once when the cog is loaded and configures Pillow so that
    extremely large / malicious images raise an exception early instead of
    being fully decompressed into memory.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        image_guard.enable_global_pillow_guard()
        log.info("[image-guard-overlay] Pillow image guard initialized")


async def setup(bot: commands.Bot) -> None:  # pragma: no cover - discord loader entry
    await bot.add_cog(PillowImageGuardOverlay(bot))
