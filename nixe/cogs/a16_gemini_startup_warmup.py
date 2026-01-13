# -*- coding: utf-8 -*-
"""
a16_gemini_startup_warmup (safe)
--------------------------------
Startup warmup helper.

Project policy:
- Google Gemini is reserved for translate / OCR-translate flows only.
- LPG must be Groq-only; do not warm LPG burst here.

Env:
  GEMINI_WARMUP_ON_BOOT=0        # set 1 to enable
  GEMINI_WARMUP_TIMEOUT_MS=12000 # per-request budget
"""

from __future__ import annotations
import os, asyncio, logging
from discord.ext import commands

log = logging.getLogger(__name__)

# Tiny 1x1 PNG for warmup calls (if enabled)
_DUMMY_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
    b'\x00\x00\x00\x0cIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\x0d\n\x2d\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
)

class GeminiStartupWarmup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.task: asyncio.Task | None = None

    async def cog_load(self):
        if (os.getenv("GEMINI_WARMUP_ON_BOOT", "0") or "").strip() != "1":
            log.info("[gemini-warmup] skipped (GEMINI_WARMUP_ON_BOOT=0)")
            return
        self.task = asyncio.create_task(self._do_warmup(), name="gemini-warmup")

    async def _do_warmup(self):
        # Warm translate pathways only (no LPG burst).
        timeout_ms = int(os.getenv("GEMINI_WARMUP_TIMEOUT_MS", "12000") or "12000")
        try:
            from nixe.helpers.translate_bridge import ocr_image_to_text  # translate OCR
        except Exception as e:
            log.warning("[gemini-warmup] translate_bridge import failed: %r", e)
            return

        try:
            await asyncio.wait_for(ocr_image_to_text(_DUMMY_PNG, lang="en"), timeout=timeout_ms/1000.0)
            log.info("[gemini-warmup] done")
        except Exception as e:
            log.warning("[gemini-warmup] err=%r", e)

    def cog_unload(self):
        try:
            if self.task and not self.task.done():
                self.task.cancel()
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(GeminiStartupWarmup(bot))
