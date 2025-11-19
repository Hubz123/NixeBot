
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, logging, asyncio, discord
from discord.ext import commands
from nixe.state_runtime import get_phash_ids
from nixe.helpers.img_hashing import phash_list_from_bytes, dhash_list_from_bytes

log = logging.getLogger(__name__)

MARKER = (os.getenv("PHASH_DB_MARKER") or "NIXE_PHASH_DB_V1").strip()
SRC_THREAD_ID = int(
    os.getenv("PHASH_IMAGEPHISH_THREAD_ID")
    or os.getenv("NIXE_PHASH_SOURCE_THREAD_ID", "0")
    or 0
)
SRC_THREAD_NAME = (os.getenv("NIXE_PHASH_SOURCE_THREAD_NAME") or "imagephising").lower()
DEST_THREAD_ID = int(
    os.getenv("PHASH_DB_THREAD_ID")
    or os.getenv("NIXE_PHASH_DB_THREAD_ID", "0")
    or 0
)
DEST_MSG_ID = int(os.getenv("PHASH_DB_MESSAGE_ID", "0") or 0)
LOG_CH_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
NO_FALLBACK = (os.getenv("NIXE_PHASH_DISABLE_LOG_FALLBACK", "1") == "1")

class PhashImagephisingWatcher(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = self.bot.loop.create_task(self._bootstrap())

    def cog_unload(self):
        try: self._task.cancel()
        except Exception: pass

    async def _bootstrap(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(1)
        tag = "(no-fallback)" if NO_FALLBACK else f"(fallback log={LOG_CH_ID})"
        tid, mid = get_phash_ids()
        log.info("[phash-inbox] target dest id=%s %s", tid or DEST_THREAD_ID, tag)

    def _is_src(self, ch: discord.abc.GuildChannel) -> bool:
        try:
            if isinstance(ch, discord.Thread):
                if SRC_THREAD_ID and ch.id == SRC_THREAD_ID:
                    return True
                if not SRC_THREAD_ID and ch.name and ch.name.lower() == SRC_THREAD_NAME:
                    return True
        except Exception:
            pass
        return False

    async def _get_dest(self) -> discord.abc.GuildChannel | None:
        tid, mid = get_phash_ids()
        dynamic_dest = int(tid or DEST_THREAD_ID or 0)
        if not dynamic_dest:
            return None
        try:
            d = self.bot.get_channel(dynamic_dest) or await self.bot.fetch_channel(dynamic_dest)
            if isinstance(d, discord.Thread) and getattr(d, "archived", False):
                try:
                    await d.edit(archived=False, locked=False, reason="auto-unarchive phash db thread")
                except Exception:
                    pass
            return d if isinstance(d, (discord.Thread, discord.TextChannel)) else None
        except Exception:
            return None

    async def _commit(self, dest: discord.abc.GuildChannel, board_msg_id: int, phashes: list[str], dhashes: list[str]):
        # Collect-only watcher; merging handled by board cogs.
        log.debug("[phash-inbox] captured %d p and %d d for board %s", len(phashes), len(dhashes), board_msg_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message or not message.guild or message.author.bot:
            return
        ch = message.channel
        if not self._is_src(ch):
            return
        attchs = getattr(message, "attachments", None) or ()
        if not attchs:
            return

        dest = await self._get_dest()
        if not dest:
            if not NO_FALLBACK and LOG_CH_ID:
                try:
                    d = self.bot.get_channel(LOG_CH_ID) or await self.bot.fetch_channel(LOG_CH_ID)
                    dest = d if isinstance(d, (discord.Thread, discord.TextChannel)) else None
                except Exception:
                    dest = None
        if not dest:
            return

        uniq_p, uniq_d = set(), set()
        cur_p, cur_d = [], []
        for att in attchs:
            try:
                raw = await att.read()
            except Exception:
                raw = b""
            if not raw:
                continue
            for h in phash_list_from_bytes(raw, max_frames=6):
                if h not in uniq_p:
                    uniq_p.add(h); cur_p.append(h)
            try:
                for h in dhash_list_from_bytes(raw, max_frames=6):
                    if h not in uniq_d:
                        uniq_d.add(h); cur_d.append(h)
            except Exception:
                pass
        tid, mid = get_phash_ids()
        runtime_msg = int(mid or DEST_MSG_ID or 0)
        if (cur_p or cur_d) and runtime_msg:
            await self._commit(dest, runtime_msg, cur_p, cur_d)

async def setup(bot: commands.Bot):
    await bot.add_cog(PhashImagephisingWatcher(bot))

def legacy_setup(bot: commands.Bot):
    bot.add_cog(PhashImagephisingWatcher(bot))
