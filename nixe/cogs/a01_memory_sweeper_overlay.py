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


def _is_render() -> bool:
    # Render sets several environment variables. We check multiple to reduce false positives.
    for k in ("RENDER", "RENDER_SERVICE_ID", "RENDER_INSTANCE_ID", "RENDER_EXTERNAL_URL"):
        if os.getenv(k):
            return True
    return False


def _runtime_profile() -> str:
    # Nixe uses NIXE_RUNTIME_PROFILE / RUNTIME_PROFILE for miniPC deployments.
    # Values: "minipc" | "default" | others.
    v = (os.getenv("NIXE_RUNTIME_PROFILE") or os.getenv("RUNTIME_PROFILE") or "").strip().lower()
    return v







def _cgroup_mem_limit_mb() -> int | None:
    """Best-effort cgroup memory limit detection (MB). Returns None if unlimited/unknown."""
    paths = [
        ("/sys/fs/cgroup/memory.max", "v2"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "v1"),
    ]
    for path, _ in paths:
        try:
            v = (open(path, "r", encoding="utf-8").read() or "").strip()
        except Exception:
            continue
        if not v or v == "max":
            continue
        if v.isdigit():
            b = int(v)
            # ignore absurd "unlimited" values
            if b > 0 and b < (1 << 60):
                return max(1, int(b / 1024 / 1024))
    return None

class MemorySweeperOverlay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False
        self._busy = False

        # Auto-profile: choose sane defaults based on where we're running.
        # - Render (constrained containers): protect hard (default cap 512MB)
        # - miniPC profile: moderate cap (default 2048MB)
        # - PC/local default: generous cap (default 4096MB)
        #
        # Any explicit env values always win.
        self.auto_profile = _i("NIXE_RAM_AUTO_PROFILE", 1) != 0
        cap_env_set = os.getenv("NIXE_RAM_CAP_MB") is not None
        trim_env_set = os.getenv("NIXE_RAM_TRIM_MB") is not None
        aggr_env_set = os.getenv("NIXE_RAM_TRIM_AGGRESSIVE_MB") is not None
        check_env_set = os.getenv("NIXE_RAM_CHECK_SEC") is not None

        self.cap_mb = _i("NIXE_RAM_CAP_MB", 0)

        prof = _runtime_profile()
        is_render = _is_render()
        is_minipc = (prof == "minipc")

        if self.auto_profile:
            if is_render:
                # Render: default to container memory limit if detectable; otherwise fall back to 512MB.
                if self.cap_mb <= 0 and not cap_env_set:
                    lim = _cgroup_mem_limit_mb()
                    self.cap_mb = lim if lim else 512
            elif is_minipc:
                # miniPC: if unset (or accidentally left at Render-tuned 512), upgrade to 2GB.
                if (self.cap_mb <= 0 and not cap_env_set) or self.cap_mb == 512:
                    self.cap_mb = 2048
            else:
                # PC/local: if unset (or accidentally left at Render-tuned 512), upgrade to 4GB.
                if (self.cap_mb <= 0 and not cap_env_set) or self.cap_mb == 512:
                    self.cap_mb = 4096

        # Threshold defaults:

        # - If user explicitly set trim/aggr, honor them.
        # - Otherwise compute from cap on_ready.
        self.trim_mb = _i("NIXE_RAM_TRIM_MB", 0 if not trim_env_set else 440)
        self.aggr_mb = _i("NIXE_RAM_TRIM_AGGRESSIVE_MB", 0 if not aggr_env_set else 480)

        force_thr = (os.getenv("NIXE_RAM_FORCE_THRESHOLDS") or "").strip() in ("1","true","TRUE","yes","YES")
        # If we're NOT on Render and the env thresholds look like Render-tuned leftovers (e.g., 430/450MB),
        # ignore them and recompute from cap in on_ready. Set NIXE_RAM_FORCE_THRESHOLDS=1 to force honoring env.
        if (not is_render) and self.auto_profile and (not force_thr) and (self.cap_mb >= 2048):
            if trim_env_set and self.trim_mb > 0 and self.trim_mb < max(512, int(self.cap_mb * 0.5)):
                self.trim_mb = 0
            if aggr_env_set and self.aggr_mb > 0 and self.aggr_mb < max(640, int(self.cap_mb * 0.6)):
                self.aggr_mb = 0

        # Check interval (seconds). Default depends on environment unless explicitly set.
        if check_env_set:
            self.check_sec = _i("NIXE_RAM_CHECK_SEC", 20)
        else:
            if is_render:
                self.check_sec = 10
            elif is_minipc:
                self.check_sec = 15
            else:
                self.check_sec = 30

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
