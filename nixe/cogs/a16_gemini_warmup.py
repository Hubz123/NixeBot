from __future__ import annotations

import asyncio
import os
from discord.ext import commands


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


class GeminiWarmup(commands.Cog):
    """Warmup hook that is safe for dry-run.

    Important:
    - Do NOT start tasks in __init__ (smoketest may create bot without login)
    - Start only after on_ready
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._started: bool = False

    def cog_unload(self):
        if self._task and not self._task.done():
            self._task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if self._started:
            return
        self._started = True

        # Both gates must allow warmup.
        if not _env_bool("GEMINI_WARMUP_ENABLE", True):
            return
        if not _env_bool("GEMINI_WARMUP_ON_BOOT", False):
            return

        self._task = asyncio.create_task(self._run())

    async def _run(self):
        await self.bot.wait_until_ready()
        try:
            tout = int(os.getenv("LUCKYPULL_GEM_TIMEOUT_MS", "20000"))
        except Exception:
            tout = 20000

        # Best-effort warmup (optional). Never crash the bot.
        try:
            from nixe.helpers import gemini_bridge
            warm = getattr(gemini_bridge, "_warmup", None)
            if warm:
                await warm(timeout_ms=tout)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(GeminiWarmup(bot))
