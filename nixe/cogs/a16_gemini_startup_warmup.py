# -*- coding: utf-8 -*-
"""
a16_gemini_startup_warmup (v2)
- Priming Gemini at startup to avoid first-call timeouts on cold env (Render Free, etc).
- Warms both keys (GEMINI_API_KEY, GEMINI_API_KEY_B) using the same image path as production classify.
- Does NOT change output formats; it's side-effect-only and silent.
Env:
  GEMINI_WARMUP_ON_BOOT=1        # set 0 to disable
  GEMINI_WARMUP_TIMEOUT_MS=12000 # per request budget for warmup
"""
from __future__ import annotations
import os, asyncio, logging
from discord.ext import commands

log = logging.getLogger(__name__)

# 1x1 transparent PNG to avoid payload overhead
_TINY_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\x9cc`\x00'
    b'\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82'
)

class GeminiStartupWarmup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.task = None

    async def cog_load(self):
        if os.getenv("GEMINI_WARMUP_ON_BOOT", "1") != "1":
            log.info("[gemini-warmup] skipped (GEMINI_WARMUP_ON_BOOT=0)")
            return
        self.task = asyncio.create_task(self._do_warmup(), name="gemini-warmup")

    async def _do_warmup(self):
        try:
            # prefer burst (identik dengan prod path)
            from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes_burst as _classify
        except Exception:
            try:
                from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes as _classify
            except Exception as e:
                log.warning("[gemini-warmup] no classify function available: %r", e)
                return

        timeout_ms = 0
        try:
            timeout_ms = int(os.getenv("GEMINI_WARMUP_TIMEOUT_MS", "12000"))
        except Exception:
            timeout_ms = 12000

        # Run two warmups in sequence to fully establish TLS + cache
        for i in range(2):
            try:
                # Use a small payload; result ignored
                res = await _classify(_TINY_PNG)  # (lucky, score, via, reason) expected
                log.info("[gemini-warmup] pass=%d via=%s reason=%s", i+1, res[2] if len(res) > 2 else "?", res[3] if len(res) > 3 else "?")
            except asyncio.TimeoutError:
                log.warning("[gemini-warmup] pass=%d timeout (ok to ignore)", i+1)
            except Exception as e:
                log.warning("[gemini-warmup] pass=%d err=%r (continue)", i+1, e)
            # tiny delay to let TCP reuse settle
            await asyncio.sleep(0.25)

        log.info("[gemini-warmup] done")

    def cog_unload(self):
        try:
            if self.task and not self.task.done():
                self.task.cancel()
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(GeminiStartupWarmup(bot))
