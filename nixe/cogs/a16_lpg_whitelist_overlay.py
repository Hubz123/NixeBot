    import os, asyncio, logging
    import discord
    from discord.ext import commands
    from nixe.helpers import sticky_board

    ENABLE = os.getenv("LPG_WHITELIST_OVERLAY_ENABLE","1") == "1"
    NO_NEW_THREADS = os.getenv("LPG_WHITELIST_NO_NEW_THREADS","1") == "1"

    class LuckyPullWhitelistOverlay(commands.Cog):
        def __init__(self, bot: commands.Bot):
            self.bot = bot
            self.board = sticky_board.StickyBoard(bot, "LPG_WHITELIST_THREAD_ID", "[LPG-WHITELIST]", "Lucky Pull Whitelist")
            self._items = set()
            self._bg = self.bot.loop.create_task(self._periodic_update())

        async def cog_unload(self):
            try:
                self._bg.cancel()
            except Exception:
                pass

        async def _periodic_update(self):
            await self.bot.wait_until_ready()
            while not self.bot.is_closed():
                try:
                    if ENABLE:
                        lines = sorted(self._items)
                        if not lines:
                            lines = ["(kosong)"]
                        await self.board.update_lines(lines, footer="Whitelist sticky; no new threads.")
                except Exception as e:
                    logging.warning("[lpg_whitelist] update failed: %s", e)
                await asyncio.sleep(60)

        # Example admin command to add whitelist tokens (image hash prefix, user id, etc.)
        @commands.command(name="lpg_whitelist_add")
        @commands.has_permissions(manage_guild=True)
        async def lpg_whitelist_add(self, ctx: commands.Context, *, token: str):
            if not ENABLE:
                return await ctx.reply("Whitelist overlay nonaktif.")
            self._items.add(token.strip())
            await ctx.reply(f"Ditambahkan ke whitelist: `{token.strip()}`")

        @commands.command(name="lpg_whitelist_list")
        @commands.has_permissions(manage_guild=True)
        async def lpg_whitelist_list(self, ctx: commands.Context):
            if not ENABLE:
                return await ctx.reply("Whitelist overlay nonaktif.")
            view = "\n".join(sorted(self._items) or ["(kosong)"])
            await ctx.reply(f"```
{view}
```")

    async def setup(bot: commands.Bot):
        await bot.add_cog(LuckyPullWhitelistOverlay(bot))
