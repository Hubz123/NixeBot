# nixe/cogs/a00_lpa_disable_overlay.py
import os, logging, asyncio
from discord.ext import commands

def _env_bool(name: str, default=False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1","true","yes","on","y")

class LPAutoDisableOverlay(commands.Cog):
    """If LPA_DISABLE=1, unload lucky_pull_auto and (re)load legacy luckypull_guard."""
    def __init__(self, bot):
        self.bot = bot
        self.log = logging.getLogger(__name__)
        self._task = None
        self._started = False
    @commands.Cog.listener()
    async def on_ready(self):
        if getattr(self, '_started', False):
            return
        self._started = True
        try:
            self._task = asyncio.create_task(self._apply())
        except Exception:
            self._task = None



    async def _apply(self):
        await self.bot.wait_until_ready()
        if not _env_bool("LPA_DISABLE", False):
            self.log.info("[lpa-disable] LPA_DISABLE not set -> no action")
            return
        # Unload LPA if present
        try:
            await self.bot.unload_extension("nixe.cogs.lucky_pull_auto")
            self.log.warning("[lpa-disable] unloaded: nixe.cogs.lucky_pull_auto")
        except Exception as e:
            self.log.info(f"[lpa-disable] lucky_pull_auto absent: {e}")
        # Ensure legacy guard loaded
        try:
            await self.bot.load_extension("nixe.cogs.luckypull_guard")
            self.log.warning("[lpa-disable] loaded: nixe.cogs.luckypull_guard")
        except Exception as e:
            self.log.info(f"[lpa-disable] luckypull_guard load note: {e}")

async def setup(bot):
    await bot.add_cog(LPAutoDisableOverlay(bot))
