from __future__ import annotations
import os, asyncio, logging, base64
from typing import Optional
from discord.ext import commands, tasks

log = logging.getLogger("nixe.cogs.a16_gemini_keepalive_overlay")

# 1x1 transparent PNG
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/ZWl2t0AAAAASUVORK5CYII="
)

class GeminiKeepAlive(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = (os.getenv("GEMINI_WARMUP_ENABLE","1") == "1")
        try:
            self.interval = int(os.getenv("GEMINI_KEEPALIVE_SEC","300"))
        except Exception:
            self.interval = 300
        log.info("[gemini-keepalive] enabled=%s interval=%ss", self.enabled, self.interval)

    @tasks.loop(count=1)
    async def _bootstrap(self):
        await asyncio.sleep(1.0)
        if self.enabled:
            try:
                from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes
                await classify_lucky_pull_bytes(_PNG_1x1, context="warmup")
                log.info("[gemini-keepalive] initial warmup done")
            except Exception as e:
                log.debug("[gemini-keepalive] initial warmup skipped: %r", e)
        if self.enabled and self.interval > 0:
            self._periodic.change_interval(seconds=float(self.interval))
            if not self._periodic.is_running():
                self._periodic.start()

    @tasks.loop(seconds=600.0)
    async def _periodic(self):
        if not self.enabled:
            return
        try:
            from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes
            await classify_lucky_pull_bytes(_PNG_1x1, context="keepalive")
            log.debug("[gemini-keepalive] tick")
        except Exception as e:
            log.debug("[gemini-keepalive] tick skipped: %r", e)

    @_bootstrap.before_loop
    @_periodic.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener("on_ready")
    async def on_ready(self):
        if self.enabled and not self._bootstrap.is_running():
            self._bootstrap.start()

async def setup(bot: commands.Bot):
    await bot.add_cog(GeminiKeepAlive(bot))
