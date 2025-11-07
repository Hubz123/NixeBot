# nixe/cogs/a16_gemini_cleanup_overlay.py
from __future__ import annotations
import inspect
from discord.ext import commands

class GeminiCleanupOverlay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        try:
            import nixe.helpers.gemini_http_cleanup  # register atexit
        except Exception:
            pass

    def cog_unload(self):
        try:
            from nixe.helpers.gemini_http_cleanup import close_now
            fut = close_now()
            if inspect.isawaitable(fut):
                self.bot.loop.create_task(fut)
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(GeminiCleanupOverlay(bot))
