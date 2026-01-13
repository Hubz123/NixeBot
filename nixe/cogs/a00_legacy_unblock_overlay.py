# nixe/cogs/a00_legacy_unblock_overlay.py
import os, logging, asyncio
from discord.ext import commands

def _env_bool(name: str, default=False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1","true","yes","on","y")

BLOCKERS = [
    "nixe.cogs.a00_block_legacy_gacha_guards",
    "nixe.cogs.a00_block_legacy_gacha_guards_plus",
]

class LegacyUnblockOverlay(commands.Cog):
    """When LPA_DISABLE=1, remove blockers so legacy luckypull_guard stays loaded."""
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
            self.log.info("[legacy-unblock] LPA_DISABLE not set -> keep blockers")
            return
        for ext in BLOCKERS:
            try:
                await self.bot.unload_extension(ext)
                self.log.warning(f"[legacy-unblock] unloaded blocker: {ext}")
            except Exception as e:
                self.log.info(f"[legacy-unblock] blocker absent: {ext} ({e})")

async def setup(bot):
    await bot.add_cog(LegacyUnblockOverlay(bot))
