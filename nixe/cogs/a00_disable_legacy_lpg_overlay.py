"""
a00_disable_legacy_lpg_overlay
------------------------------
Strong dedup overlay to ensure legacy Lucky Pull cogs never double-send messages.
- Unloads legacy LPG guards/loaders (and lucky_pull_auto) on connect.
- Patches load_extension so future attempts to load them are blocked.
- Controlled by env: LPG_DEDUP_DISABLE_LEGACY (default=1 -> enabled)
"""
import os, asyncio, inspect, logging
from typing import Iterable
from discord.ext import commands

TARGET_EXTS: Iterable[str] = (
    "nixe.cogs.lucky_pull_guard",
    "nixe.cogs.gacha_luck_guard",
    "nixe.cogs.gacha_luck_guard_random_only",
    "nixe.cogs.a15_lucky_pull_guard_loader",
    "nixe.cogs.a15_lucky_pull_auto_loader",
    "nixe.cogs.lucky_pull_auto",
)

class DisableLegacyLPG(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = os.getenv("LPG_DEDUP_DISABLE_LEGACY", "1") == "1"
        self.log = logging.getLogger("nixe.cogs.a00_disable_legacy_lpg_overlay")
        if self.enabled:
            self.log.warning("[lpg-dedup] overlay enabled (unload + block legacy LPG + auto)")
            self._patch_load_extension()

    def _patch_load_extension(self):
        bot = self.bot
        if getattr(bot, "_lpg_dedup_patched", False):
            return
        original = bot.load_extension

        async def guarded_load(name: str, *a, **k):
            if name in TARGET_EXTS:
                self.log.warning("[lpg-dedup] blocked load_extension(%s)", name)
                return
            res = original(name, *a, **k)
            if inspect.isawaitable(res):
                return await res
            return res

        bot.load_extension = guarded_load  # type: ignore[attr-defined]
        bot._lpg_dedup_patched = True  # type: ignore[attr-defined]

    async def _safe_unload(self, name: str):
        if name not in self.bot.extensions:
            return
        try:
            res = self.bot.unload_extension(name)
            if inspect.isawaitable(res):
                await res
            self.log.warning("[lpg-dedup] unloaded extension: %s", name)
        except Exception as e:
            self.log.warning("[lpg-dedup] unload failed for %s: %r", name, e)

    async def _do_unload_all(self):
        if not self.enabled:
            return
        await asyncio.sleep(0.1)
        for ext in ("nixe.cogs.a15_lucky_pull_guard_loader", "nixe.cogs.a15_lucky_pull_auto_loader"):
            await self._safe_unload(ext)
        for ext in (
            "nixe.cogs.lucky_pull_guard",
            "nixe.cogs.gacha_luck_guard",
            "nixe.cogs.gacha_luck_guard_random_only",
            "nixe.cogs.lucky_pull_auto",
        ):
            await self._safe_unload(ext)

    @commands.Cog.listener()
    async def on_ready(self):
        await self._do_unload_all()

async def setup(bot: commands.Bot):
    await bot.add_cog(DisableLegacyLPG(bot))
