#!/usr/bin/env python3
import logging
from discord.ext import commands
from nixe.helpers.lpg_thread_guard import ensure_sticky_thread, ensure_single_pinned

MARKER = "<nixe:lpg:sticky:v1>"

class LPGThreadEnforcer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        th = await ensure_sticky_thread(self.bot)
        if th is None:
            logging.error("[lpg-thread] FAILED to ensure sticky thread")
            return
        ok = await ensure_single_pinned(th, MARKER)
        if ok:
            logging.info("[lpg-thread] sticky OK in thread %s (%s)", th.name, th.id)
        else:
            logging.error("[lpg-thread] sticky FAILED in thread %s (%s)", th.name, th.id)

async def setup(bot: commands.Bot):
    await bot.add_cog(LPGThreadEnforcer(bot))
