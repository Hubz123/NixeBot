# -*- coding: utf-8 -*-
"""a01_memory_sweeper_overlay

Render Free plan (and other constrained containers) can hard-kill the process
when RSS exceeds the platform limit. This overlay runs a lightweight periodic
memory sweep that clears common caches to reduce RSS.

It is intentionally non-intrusive:
- never blocks message handling
- never exits/restarts the process
- best-effort only (fails open)
"""

from __future__ import annotations

import asyncio
import logging
import os

from discord.ext import commands, tasks

from nixe.helpers.memory_sweeper import rss_mb, sweep


log = logging.getLogger(__name__)


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except Exception:
        return int(default)


class MemorySweeperOverlay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False
        self._busy = False

        self.cap_mb = _i("NIXE_RAM_CAP_MB", 0)
        # Defaults tuned for 512MB Render: start sweeping before hard limit.
        self.trim_mb = _i("NIXE_RAM_TRIM_MB", 440)
        self.aggr_mb = _i("NIXE_RAM_TRIM_AGGRESSIVE_MB", 480)
        self.check_sec = _i("NIXE_RAM_CHECK_SEC", 20)
        try:
            self._watch_loop.change_interval(seconds=max(5, self.check_sec))
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        if self._started:
            return
        self._started = True

        if self.cap_mb <= 0:
            # Disabled by config.
            return

        # Ensure thresholds are sane.
        if self.aggr_mb <= 0:
            self.aggr_mb = max(1, int(self.cap_mb * 0.94))
        if self.trim_mb <= 0:
            self.trim_mb = max(1, int(self.cap_mb * 0.86))

        # If user configured weird values, enforce ordering.
        if self.trim_mb >= self.aggr_mb:
            self.trim_mb = max(1, self.aggr_mb - 20)

        log.warning(
            "[mem-sweep] enabled cap=%dMB trim=%dMB aggressive=%dMB every=%ds",
            self.cap_mb,
            self.trim_mb,
            self.aggr_mb,
            self.check_sec,
        )

        try:
            self._watch_loop.start()
        except Exception:
            pass

    @tasks.loop(seconds=20)
    async def _watch_loop(self):
        # Prevent overlapping sweeps.
        if self._busy:
            return
        self._busy = True
        try:
            cur = rss_mb()
            if cur <= 0:
                return

            # Aggressive sweep when close to cap.
            if cur >= float(self.aggr_mb):
                sweep(self.bot, aggressive=True)
            elif cur >= float(self.trim_mb):
                sweep(self.bot, aggressive=False)
        except Exception:
            # Fail open.
            return
        finally:
            # Give event loop breathing room after a sweep.
            try:
                await asyncio.sleep(0)
            except Exception:
                pass
            self._busy = False


async def setup(bot: commands.Bot):
    await bot.add_cog(MemorySweeperOverlay(bot))
