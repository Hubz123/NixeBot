
# -*- coding: utf-8 -*-
from __future__ import annotations

import os, asyncio, json, logging, discord
from typing import Set, Optional
from discord.ext import commands
from nixe.state_runtime import get_phash_ids

from nixe.helpers import img_hashing
from nixe.helpers.phash_board import get_pinned_db_message, edit_pinned_db

log = logging.getLogger(__name__)

SRC_THREAD_ID = int(
    os.getenv("PHASH_IMAGEPHISH_THREAD_ID")
    or os.getenv("NIXE_PHASH_SOURCE_THREAD_ID", "0")
    or 0
)
SRC_THREAD_NAME = (os.getenv("NIXE_PHASH_SOURCE_THREAD_NAME") or "imagephising").lower()
DB_THREAD_ID = int(
    os.getenv("PHASH_DB_THREAD_ID")
    or os.getenv("NIXE_PHASH_DB_THREAD_ID", "0")
    or 0
)
DB_MSG_ID = int(os.getenv("PHASH_DB_MESSAGE_ID", "0") or 0)
MAX_FRAMES = int(os.getenv("PHASH_MAX_FRAMES", "6"))
AUTO_BACKFILL = (
    os.getenv("PHASH_RESYNC_ON_BOOT")
    or os.getenv("PHASH_RESCANNER_ENABLE")
    or os.getenv("NIXE_PHASH_AUTOBACKFILL", "0")
) == "1"
BACKFILL_LIMIT = int(os.getenv("NIXE_PHASH_BACKFILL_LIMIT", "0") or 0)
REQ_PERM = (os.getenv("PHASH_RESCAN_REQUIRE_PERM", "1") == "1")

IMAGE_EXTS = (".png",".jpg",".jpeg",".webp",".gif",".bmp",".tif",".tiff",".heic",".heif")

def _extract(text: str) -> Set[str]:
    try:
        s = text.find("```json")
        if s < 0: return set()
        s = text.find("{", s)
        e = text.find("```", s)
        data = json.loads(text[s:e])
        arr = data.get("phash") or []
        return set(str(x) for x in arr if isinstance(x, str))
    except Exception:
        return set()

def _is_src(ch: discord.abc.GuildChannel) -> bool:
    try:
        if isinstance(ch, discord.Thread):
            if SRC_THREAD_ID and ch.id == SRC_THREAD_ID: return True
            if not SRC_THREAD_ID and ch.name and ch.name.lower() == SRC_THREAD_NAME: return True
    except Exception:
        pass
    return False

class PhashRescanner(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._ready = False
        self._backfill_once = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._ready: return
        self._ready = True
        await asyncio.sleep(1.0)
        tid, mid = get_phash_ids()
        log.info("[phash-rescanner] source=%s db_thread=%s db_msg=%s", SRC_THREAD_ID or SRC_THREAD_NAME, tid or DB_THREAD_ID, mid or DB_MSG_ID)
        if AUTO_BACKFILL and not self._backfill_once:
            self._backfill_once = True
            try:
                await self._run_backfill(BACKFILL_LIMIT or None)
            except Exception:
                log.exception("[phash-rescanner] autobackfill failed")


    async def _resolve_board_tokens(self) -> Set[str]:
        msg = await get_pinned_db_message(self.bot)
        existing: Set[str] = set()
        if msg and (msg.content or ""):
            existing |= _extract(msg.content)
        return existing

    async def _run_backfill(self, limit: Optional[int]):
        # resolve source thread
        src: Optional[discord.Thread] = None
        try:
            if SRC_THREAD_ID:
                ch = self.bot.get_channel(SRC_THREAD_ID) or await self.bot.fetch_channel(SRC_THREAD_ID)
                if isinstance(ch, discord.Thread):
                    src = ch
            else:
                tid, mid = get_phash_ids()
                rtid = tid or DB_THREAD_ID
                dbth = self.bot.get_channel(rtid) or await self.bot.fetch_channel(rtid)
                parent = getattr(dbth, "parent", None)
                if parent:
                    async for t in parent.archived_threads(limit=200, private=False):
                        if t.name and t.name.lower() == SRC_THREAD_NAME:
                            src = t; break
                    if not src:
                        for t in getattr(parent, "threads", []):
                            if t.name and t.name.lower() == SRC_THREAD_NAME:
                                src = t; break
        except Exception:
            src = None
        if not isinstance(src, discord.Thread):
            log.warning("[phash-rescanner] source thread not found")
            return

        existing = await self._resolve_board_tokens()

        new_tokens: Set[str] = set()
        async for m in src.history(limit=limit):
            for att in getattr(m, "attachments", ()) or ():
                nm = (att.filename or "").lower()
                if not any(nm.endswith(ext) for ext in IMAGE_EXTS): 
                    continue
                try:
                    raw = await att.read()
                except Exception:
                    raw = b""
                if not raw:
                    continue
                for h in img_hashing.phash_list_from_bytes(raw, max_frames=MAX_FRAMES):
                    if h not in existing:
                        new_tokens.add(h)

        if not new_tokens:
            log.info("[phash-rescanner] backfill: nothing new")
            return

        merged = existing | new_tokens
        ok = await edit_pinned_db(self.bot, merged)
        log.info("[phash-rescanner] backfill merge: +%d -> %s", len(new_tokens), "OK" if ok else "SKIPPED")

    # permissions decorator (toggleable)
    if REQ_PERM:
        dec_perms = commands.has_guild_permissions(manage_messages=True)
    else:
        def dec_perms(func): return func

    @commands.guild_only()
    @commands.command(name="phash_rescan", aliases=["phash-rescan","phashrescan","rescanphash","pr"])
    @dec_perms
    async def phash_rescan_cmd(self, ctx: commands.Context, limit: int = 0):
        # quick reaction feedback even if send fails
        try:
            await ctx.message.add_reaction("🔄")
        except Exception:
            pass
        msg = await get_pinned_db_message(self.bot)
        if not msg:
            try:
                await ctx.reply("PHASH DB board belum ditemukan. Jalankan `&phash-seed here` di thread DB dulu.", mention_author=False)
            except Exception:
                pass
            return
        try:
            await ctx.reply("Starting rescan...", mention_author=False)
        except Exception:
            pass
        try:
            await self._run_backfill(limit or None)
        except Exception as e:
            log.exception("[phash-rescanner] rescan failed: %s", e)
            try:
                await ctx.reply(f"Rescan failed: {e!r}", mention_author=False)
            except Exception:
                pass
            return
        try:
            await ctx.reply("Rescan done.", mention_author=False)
        except Exception:
            pass
        try:
            await ctx.message.add_reaction("✅")
        except Exception:
            pass

    @phash_rescan_cmd.error
    async def _rescan_err(self, ctx, error):
        try:
            from discord.ext.commands import MissingPermissions, CheckFailure
        except Exception:
            MissingPermissions = CheckFailure = Exception
        if isinstance(error, MissingPermissions) or isinstance(error, CheckFailure):
            try:
                await ctx.reply("Kamu tidak punya izin untuk menjalankan rescan (butuh Manage Messages).", mention_author=False)
            except Exception:
                pass
        else:
            try:
                await ctx.reply(f"Gagal: {error!r}", mention_author=False)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Incremental merge for new messages in source thread
        if not message or not message.guild or message.author.bot:
            return
        ch = message.channel
        if not _is_src(ch):
            return

        new_tokens: Set[str] = set()
        for att in getattr(message, "attachments", ()) or ():
            nm = (att.filename or "").lower()
            if not any(nm.endswith(ext) for ext in IMAGE_EXTS):
                continue
            try:
                raw = await att.read()
            except Exception:
                raw = b""
            if not raw:
                continue
            for h in img_hashing.phash_list_from_bytes(raw, max_frames=MAX_FRAMES):
                new_tokens.add(h)

        if not new_tokens:
            return

        existing = await self._resolve_board_tokens()
        if not existing and not (await get_pinned_db_message(self.bot)):
            # no board yet; skip
            return

        merged = existing | new_tokens
        await edit_pinned_db(self.bot, merged)

async def setup(bot: commands.Bot):
    await bot.add_cog(PhashRescanner(bot))
