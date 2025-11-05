# -*- coding: utf-8 -*-
"""
a00_remove_lpg_suppress_overlay
-------------------------------
Matikan penghapus-notice (a00_lpg_suppress_double_notice_overlay)
agar log tidak spam. Aman karena dedup sudah dilakukan di sumber.
"""
import logging
import os
from discord.ext import commands

TARGET = "nixe.cogs.a00_lpg_suppress_double_notice_overlay"

class RemoveLpgSuppress(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.log = logging.getLogger("nixe.cogs.a00_remove_lpg_suppress_overlay")

    @commands.Cog.listener()
    async def on_ready(self):
        suppress_on = os.getenv('LPG_SUPPRESS_GENERIC_NOTICE','1') == '1'
        if suppress_on:
            self.log.info('[lpg-clean] keeping suppress overlay loaded (LPG_SUPPRESS_GENERIC_NOTICE=1)')
            return
        if TARGET in self.bot.extensions:
            try:
                res = self.bot.unload_extension(TARGET)
                if hasattr(res, '__await__'):
                    await res
                self.log.warning('[lpg-clean] unloaded %s (user-disabled)', TARGET)
            except Exception as e:
                self.log.warning('[lpg-clean] unload failed: %r', e)

async def setup(bot: commands.Bot):
    await bot.add_cog(RemoveLpgSuppress(bot))
