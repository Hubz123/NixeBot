# nixe/cogs/a00_enable_thread_bridge_guard_overlay.py
# Ensure the minimal thread-aware LPG guard is actually loaded in live runtime.
import logging
from discord.ext import commands

TARGET = "nixe.cogs.a00_lpg_thread_bridge_guard"
log = logging.getLogger("nixe.cogs.a00_enable_thread_bridge_guard_overlay")

class EnableThreadBridgeGuard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener("on_ready")
    async def on_ready(self):
        try:
            if TARGET in self.bot.extensions:
                log.info("[lpg-autoload] %s already loaded", TARGET)
                return
            # load it
            res = self.bot.load_extension(TARGET)
            if hasattr(res, "__await__"):
                await res
            log.warning("[lpg-autoload] loaded %s", TARGET)
        except Exception as e:
            log.warning("[lpg-autoload] load failed: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(EnableThreadBridgeGuard(bot))
