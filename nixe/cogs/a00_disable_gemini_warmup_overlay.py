# -*- coding: utf-8 -*-
"""
a00_disable_gemini_warmup_overlay
- Benar-benar mematikan Gemini keepalive/warmup.
- Aman untuk free tier supaya tidak menghabiskan kuota harian hanya dari ping background.
"""
from __future__ import annotations
import os
import logging
from discord.ext import commands

log = logging.getLogger(__name__)

class DisableGeminiWarmup(commands.Cog):
    """Matikan tugas keepalive Gemini (a16_gemini_keepalive_overlay)."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Hard-disable via env; dibaca oleh a16_gemini_keepalive_overlay.
        os.environ["GEMINI_WARMUP_ENABLE"] = "0"
        log.warning("[warmup-free] GEMINI_WARMUP_ENABLE=0 (Gemini keepalive disabled)")

        # Best-effort: jika Cog keepalive sudah ter-load, hentikan task-nya.
        try:
            from nixe.cogs.a16_gemini_keepalive_overlay import GeminiKeepAlive  # type: ignore
        except Exception:
            return

        try:
            for cog in list(self.bot.cogs.values()):
                if isinstance(cog, GeminiKeepAlive):
                    for attr in ("_bootstrap", "_periodic"):
                        task = getattr(cog, attr, None)
                        try:
                            if task is not None and getattr(task, "is_running", lambda: False)():
                                task.cancel()
                        except Exception:
                            continue
                    log.warning("[warmup-free] existing GeminiKeepAlive tasks cancelled")
        except Exception as e:  # pragma: no cover
            log.warning("[warmup-free] failed to introspect GeminiKeepAlive: %r", e)

async def setup(bot):
    await bot.add_cog(DisableGeminiWarmup(bot))
