from __future__ import annotations
import discord
import os
# -*- coding: utf-8 -*-

import asyncio, logging, discord
from discord.ext import commands, tasks
from nixe.config.self_learning_cfg import (
    LOG_CHANNEL_ID, PHASH_LOG_SCAN_LIMIT, PHASH_FIRST_DELAY_SECONDS, PHASH_INTERVAL_SECONDS
)

log = logging.getLogger(__name__)

class PhashHourlyScheduler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False
        self._loop = None

    async def cog_load(self):
        self._loop = None

    @commands.Cog.listener()
    async def on_ready(self):
        if getattr(self, '_started', False):
            return
        self._started = True
        try:
            if not self.loop_collect.is_running():
                self._loop = self.loop_collect.start()
        except Exception:
            self._loop = None

    def cog_unload(self):
        if self._loop:
            self._loop.cancel()

    @tasks.loop(seconds=PHASH_INTERVAL_SECONDS)
    async def loop_collect(self):
        if getattr(self, '_log_ch', None):
            try:
                await self._log_ch.send('✅ pHash hourly tick OK')
            except Exception:
                pass
        await self.bot.wait_until_ready()
        if not LOG_CHANNEL_ID:
            log.info("[phash_hourly] skip: LOG_CHANNEL_ID not set")
            return

        ch = self.bot.get_channel(LOG_CHANNEL_ID)
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            log.warning("[phash_hourly] LOG_CHANNEL_ID invalid: %s", LOG_CHANNEL_ID)
            return

        guard = self.bot.get_cog("SelfLearningGuard")
        if guard and hasattr(guard, "_phash_reconcile"):
            parent = ch if isinstance(ch, discord.TextChannel) else ch.parent
            phish_threads = []
            try:
                # limit akan dipaksa oleh wrapper patch_collect_phash_wrapper
                changed = await guard._phash_reconcile(parent, phish_threads)  # type: ignore
                log.info("[phash_hourly] reconcile via SelfLearningGuard done (changed=%s)", changed)
                return
            except Exception as e:
                log.exception("[phash_hourly] guard reconcile failed, fallback: %s", e)

        try:
            from nixe.ml.phash_reconcile import collect_phash_from_log
            hashes = await collect_phash_from_log(ch, limit_msgs=PHASH_LOG_SCAN_LIMIT)  # type: ignore
            n = 0
            try: n = len(hashes)
            except: pass
            log.info("[phash_hourly] collected phash: ~%s entries", n)
        except Exception as e:
            log.exception("[phash_hourly] fallback collect failed: %s", e)

    @loop_collect.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(PHASH_FIRST_DELAY_SECONDS)
        log.info("[phash_hourly] started (first_delay=%ss, every=%ss, limit=%s)",
                 PHASH_FIRST_DELAY_SECONDS, PHASH_INTERVAL_SECONDS, PHASH_LOG_SCAN_LIMIT)

async def setup(bot: commands.Bot):
    await bot.add_cog(PhashHourlyScheduler(bot))