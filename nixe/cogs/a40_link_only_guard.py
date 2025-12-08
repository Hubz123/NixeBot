
from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

# Channel yang dijaga (link-only)
LINK_ONLY_CHANNEL_IDS = {
    1447483419121549352,
}

# Pola URL sederhana: http(s)://<non-spasi>
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


class LinkOnlyGuard(commands.Cog):
    """Guard untuk channel link-only.

    - Pesan user di channel ini wajib mengandung minimal satu URL http/https (chat + link boleh).
    - Pesan yang tidak mengandung URL sama sekali akan dihapus.
    - Pesan yang dikirim oleh bot ini di channel tersebut juga dihapus, sehingga output dari
      modul GROQ/Gemini tidak pernah muncul di channel link-only.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        ids_str = ", ".join(str(cid) for cid in sorted(LINK_ONLY_CHANNEL_IDS))
        log.info("[link-only-guard] enabled for channel_ids={%s}", ids_str)

    @staticmethod
    def _has_url(content: str) -> bool:
        if not content:
            return False
        return URL_RE.search(content) is not None

    async def _handle_message(self, message: discord.Message) -> None:
        # Hanya di guild
        if message.guild is None:
            return

        if message.channel.id not in LINK_ONLY_CHANNEL_IDS:
            return

        # Hapus semua pesan yang dikirim oleh bot sendiri di channel link-only
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            try:
                await message.delete()
                log.info(
                    "[link-only-guard] deleted bot message id=%s in link-only channel=%s (%s)",
                    message.id,
                    message.channel,
                    message.channel.id,
                )
            except discord.Forbidden:
                log.warning(
                    "[link-only-guard] missing Manage Messages to delete bot message in channel=%s (%s)",
                    message.channel,
                    message.channel.id,
                )
            except discord.HTTPException as exc:
                log.error(
                    "[link-only-guard] failed to delete bot message id=%s in channel=%s (%s): %r",
                    message.id,
                    message.channel,
                    message.channel.id,
                    exc,
                )
            return

        # Abaikan bot lain
        if message.author.bot:
            return

        content = (message.content or "").strip()

        # Kalau ada minimal satu URL -> biarkan (chat + link boleh)
        if self._has_url(content):
            return

        # Tidak ada URL sama sekali -> hapus
        try:
            await message.delete()
            log.info(
                "[link-only-guard] deleted non-link message id=%s author=%s (%s) channel=%s (%s) preview=%r",
                message.id,
                message.author,
                message.author.id,
                message.channel,
                message.channel.id,
                content[:80],
            )
        except discord.Forbidden:
            log.warning(
                "[link-only-guard] missing Manage Messages permission in channel=%s (%s)",
                message.channel,
                message.channel.id,
            )
        except discord.HTTPException as exc:
            log.error(
                "[link-only-guard] failed to delete message id=%s in channel=%s (%s): %r",
                message.id,
                message.channel,
                message.channel.id,
                exc,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._handle_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        await self._handle_message(after)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LinkOnlyGuard(bot))
    log.info("[link-only-guard] cog loaded")
