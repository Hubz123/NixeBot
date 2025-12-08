# -*- coding: utf-8 -*-
"""
a00_disable_gemini_warmup_overlay
- Benar-benar mematikan SEMUA warmup/keepalive Gemini:
  * a16_gemini_keepalive_overlay.GeminiKeepAlive (periodic keepalive)
  * a16_gemini_warmup.GeminiWarmup (once-after-ready warmup)
  * a16_gemini_startup_warmup.GeminiStartupWarmup (2x burst warmup di startup)
- Aman untuk free tier supaya tidak menghabiskan kuota harian hanya dari ping background.
"""
from __future__ import annotations
import os
import logging
import asyncio
from discord.ext import commands

log = logging.getLogger(__name__)

class DisableGeminiWarmup(commands.Cog):
    """Matikan semua tugas warmup/keepalive Gemini."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Hard-disable via env, supaya cog-cog lain yang patuh env ini tidak start.
        os.environ["GEMINI_WARMUP_ENABLE"] = "0"
        os.environ["GEMINI_WARMUP_ON_BOOT"] = "0"
        log.warning("[warmup-free] GEMINI_WARMUP_ENABLE=0 GEMINI_WARMUP_ON_BOOT=0 (Gemini warmup disabled)")

        # Cleanup dilakukan setelah semua cog lain ke-load dan bot ready,
        # supaya task-task warmup yang sudah terlanjur dibuat bisa dibatalkan.
        asyncio.create_task(self._cleanup_tasks(), name="warmup-free-cleanup")

    async def _cleanup_tasks(self) -> None:
        # Tunggu bot benar-benar siap, supaya semua warmup cog sudah terdaftar.
        try:
            await self.bot.wait_until_ready()
        except Exception:
            return

        try:
            from nixe.cogs.a16_gemini_keepalive_overlay import GeminiKeepAlive  # type: ignore
        except Exception:
            GeminiKeepAlive = None  # type: ignore[assignment]

        try:
            from nixe.cogs.a16_gemini_warmup import GeminiWarmup  # type: ignore
        except Exception:
            GeminiWarmup = None  # type: ignore[assignment]

        try:
            from nixe.cogs.a16_gemini_startup_warmup import GeminiStartupWarmup  # type: ignore
        except Exception:
            GeminiStartupWarmup = None  # type: ignore[assignment]

        for cog in list(self.bot.cogs.values()):
            # Periodic keepalive
            if GeminiKeepAlive is not None and isinstance(cog, GeminiKeepAlive):
                for attr in ("_bootstrap", "_periodic"):
                    task = getattr(cog, attr, None)
                    try:
                        if task is not None and getattr(task, "is_running", lambda: False)():
                            task.cancel()
                    except Exception:
                        continue
                log.warning("[warmup-free] GeminiKeepAlive tasks cancelled")

            # Once-after-ready warmup
            if GeminiWarmup is not None and isinstance(cog, GeminiWarmup):
                task = getattr(cog, "_task", None)
                try:
                    if task is not None and not task.done():
                        task.cancel()
                except Exception:
                    pass
                log.warning("[warmup-free] GeminiWarmup task cancelled")

            # Startup burst warmup (2x pass)
            if GeminiStartupWarmup is not None and isinstance(cog, GeminiStartupWarmup):
                task = getattr(cog, "task", None)
                try:
                    if task is not None and not task.done():
                        task.cancel()
                except Exception:
                    pass
                log.warning("[warmup-free] GeminiStartupWarmup task cancelled")

async def setup(bot):
    await bot.add_cog(DisableGeminiWarmup(bot))
