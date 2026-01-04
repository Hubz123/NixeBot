from __future__ import annotations

# nixe/bot/modules/discord_bot/cogs/phash_auto_ban.py

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands

# ==== Tweakables (tidak perlu ubah config lain) ====
EMBED_COLOR = 0xF4511E            # oranye, mirip "ban" accent
DELETE_AFTER_SECONDS = 3600       # auto delete 1 jam
WIB = timezone(timedelta(hours=7))
LOG_CHANNEL_CANDIDATES = (
    'nixe-only', 'nixe-only', 'nixe-only',
    "banlog", "mod-log"
)

class _LRUSet:
    def __init__(self, cap: int = 4096):
        self.cap = cap
        self._d = OrderedDict()
    def add(self, key):
        if key in self._d:
            self._d.move_to_end(key)
            return False
        self._d[key] = True
        if len(self._d) > self.cap:
            self._d.popitem(last=False)
        return True
    def __contains__(self, key):
        return key in self._d

class PhashAutoBan(commands.Cog):
    """Embed ban ala 'Test Ban (Simulasi)' + anti-duplikasi + auto delete log/asal."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._seen = _LRUSet()

    # NOTE:
    #  - Deteksi pHash tetap di modul kamu yang lama.
    #  - Panggil helper `report_ban(...)` di bawah ini saat match.

    async def _schedule_delete(self, *msgs: discord.Message):
        if DELETE_AFTER_SECONDS <= 0:
            return
        await asyncio.sleep(DELETE_AFTER_SECONDS)
        for m in msgs:
            try:
                await m.delete()
            except Exception:
                pass

    def _find_log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        for name in LOG_CHANNEL_CANDIDATES:
            ch = discord.utils.get(guild.text_channels, name=name)
            if ch:
                return ch
        return None

    async def report_ban(
        self,
        *,
        message: discord.Message,
        member: discord.Member | discord.User,
        moderator: Optional[discord.Member | discord.User] = None,
        reason: str = "Auto-ban: pHash match",
        attachment: Optional[discord.Attachment] = None,
        delete_source_after: bool = True
    ):
        """Dipanggil oleh detektor pHash saat terjadi match.
        - Kirim embed 1x ke log channel (fallback channel asal).
        - Ban user (best effort).
        - Auto-delete embed log + pesan sumber setelah 1 jam (opsional).
        """
        key = (message.guild.id if message.guild else 0, message.id, "ban")
        if key in self._seen:
            return
        self._seen.add(key)

        # Ban (best-effort)
        try:
            if message.guild:
                await message.guild.ban(member, reason=reason, delete_message_days=0)
        except Exception:
            pass

        # Rakit embed dgn gaya mirip "Test Ban (Simulasi)"
        title = "💀 Auto Ban"
        em = discord.Embed(title=title, color=EMBED_COLOR)
        em.add_field(name="Target", value=f"{getattr(member, 'mention', member)} (`{member.id}`)", inline=False)
        if moderator is None:
            # fallback: gunakan author message sebagai "pemicu"
            moderator = message.author
        em.add_field(name="Moderator", value=getattr(moderator, "mention", str(moderator)), inline=False)
        em.add_field(name="Reason", value=reason or "—", inline=False)

        em.description = "_Ini hasil deteksi otomatis. Konten sumber akan dibersihkan otomatis._"
        em.set_footer(text=f"NIXE • {datetime.now(WIB).strftime('%Y-%m-%d %H:%M:%S WIB')}")

        # Thumbnail dari attachment kalau ada (biar layout kanan seperti contoh)
        thumb_url = None
        if attachment is None and message.attachments:
            # pilih image pertama
            for a in message.attachments:
                if (a.content_type or "").startswith("image/"):
                    attachment = a
                    break
        if attachment:
            try:
                thumb_url = attachment.url
                em.set_thumbnail(url=thumb_url)
            except Exception:
                pass

        # Kirim 1x
        target = self._find_log_channel(message.guild) if message.guild else None
        if target is None:
            target = message.channel  # fallback
        sent = None
        try:
            sent = await target.send(embed=em)
        except Exception:
            try:
                sent = await target.send(f"💀 Auto Ban {getattr(member, 'mention', member)} (`{member.id}`) — {reason}")
            except Exception:
                pass

        # Auto-delete embed log + sumber setelah 1 jam
        tasks = []
        if sent:
            tasks.append(sent)
        if delete_source_after:
            tasks.append(message)
        if tasks:
            asyncio.create_task(self._schedule_delete(*tasks))

async def setup(bot: commands.Bot):
    await bot.add_cog(PhashAutoBan(bot))