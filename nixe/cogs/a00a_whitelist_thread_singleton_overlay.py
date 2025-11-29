# nixe/cogs/a00a_whitelist_thread_singleton_overlay.py
# Collapse duplicate "Whitelist LPG (FP)" threads; reuse one and archive duplicates.
import os, asyncio, logging, discord
from discord.ext import commands
_log = logging.getLogger(__name__)

def _getenv(k, d=""):
    return os.getenv(k, d)

class WhitelistThreadSingleton(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.log_chan_id = int(_getenv("LOG_CHANNEL_ID") or _getenv("NIXE_PHISH_LOG_CHAN_ID") or "0")
        self.thread_name = _getenv("LPG_WHITELIST_THREAD_NAME", "Whitelist LPG (FP)")
        self._task = None

    async def _ensure_once(self):
        if not self.log_chan_id:
            return
        ch = self.bot.get_channel(self.log_chan_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(self.log_chan_id)
            except Exception:
                return
        if not isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
            return

        chosen = None
        duplicates = []
        for th in getattr(ch, "threads", []):
            try:
                if (th.name or "").strip() == self.thread_name.strip():
                    if chosen is None:
                        chosen = th
                    else:
                        duplicates.append(th)
            except Exception:
                continue

        if chosen is None:
            try:
                if isinstance(ch, discord.TextChannel):
                    chosen = await ch.create_thread(name=self.thread_name, type=discord.ChannelType.public_thread)
                elif isinstance(ch, discord.ForumChannel):
                    post = await ch.create_thread(name=self.thread_name, content="LPG whitelist thread")
                    chosen = post
            except Exception:
                return

        for th in duplicates:
            try:
                if not th.archived:
                    await th.edit(archived=True, locked=True)
            except Exception:
                pass

        os.environ["LPG_WHITELIST_THREAD_ID"] = str(chosen.id)
        _log.info(f"[lpg-wl-singleton] chosen={chosen.id} archived={len(duplicates)} name='{self.thread_name}'")

    async def _run_periodic(self):
        await asyncio.sleep(1.5)
        await self._ensure_once()
        while True:
            await asyncio.sleep(300)
            await self._ensure_once()

    @commands.Cog.listener()
    async def on_ready(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run_periodic())

async def setup(bot: commands.Bot):
    add = getattr(bot, "add_cog")
    res = add(WhitelistThreadSingleton(bot))
    import inspect
    if inspect.isawaitable(res):
        await res
