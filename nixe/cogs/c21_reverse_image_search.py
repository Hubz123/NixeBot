
"""c21_reverse_image_search.py

Reverse image search helper (guild-only, add-only):

- Provides Message Context Menu: "Reverse image (Nixe)".
- When invoked on a message, it tries to locate the first image:
  - Attachments on that message.
  - Embeds / thumbnails on that message.
  - Attachments / embeds on the referenced message (if the message is a reply).

- It then builds ready-to-click links for common reverse image engines:
  - Google Lens
  - Bing Visual Search
  - Yandex Images
  - TinEye

No external API calls are made; Nixe only exposes the Discord CDN URL and
preformatted search URLs.

Optional configs (runtime_env.json or env):

  REVERSE_IMAGE_ENABLE=1               (default: 1)
  REVERSE_IMAGE_EPHEMERAL=1            (default: 1)
  REVERSE_IMAGE_SYNC_ON_BOOT=1         (default: 1)

These follow the same style as the translate cog.

"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from urllib.parse import quote_plus

import discord
from discord import app_commands
from discord.ext import commands


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _as_bool(key: str, default: bool = False) -> bool:
    v = _env(key, "1" if default else "0").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _first_image_url_from_message(message: discord.Message) -> Optional[str]:
    """Return the first image URL from attachments or embeds on a message."""
    # Attachments first
    for att in getattr(message, "attachments", []) or []:
        fn = (getattr(att, "filename", "") or "").lower()
        url = str(getattr(att, "url", "") or "")
        ct = (getattr(att, "content_type", "") or "").lower()
        is_img = (
            any(fn.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
            or any(url.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
            or ("image/" in ct)
        )
        if is_img and url:
            return url

    # Embeds: image or thumbnail
    for e in getattr(message, "embeds", []) or []:
        try:
            if e.image and e.image.url:
                return str(e.image.url)
        except Exception:
            pass
        try:
            if e.thumbnail and e.thumbnail.url:
                return str(e.thumbnail.url)
        except Exception:
            pass
        # Some bots put the direct image URL in embed.url
        try:
            if e.url and any(str(e.url).lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
                return str(e.url)
        except Exception:
            pass

    return None


async def _extract_image_url(message: discord.Message) -> Optional[str]:
    """Try to locate an image URL from the message or its referenced message."""
    # 1) Directly on this message
    url = _first_image_url_from_message(message)
    if url:
        return url

    # 2) If this message is a reply, try the referenced/original message
    ref = getattr(message, "reference", None)
    if ref is not None:
        # Try resolved message first
        ref_msg = getattr(ref, "resolved", None)
        if ref_msg is None:
            # Fallback to fetching by id in the same channel
            try:
                if ref.message_id and message.channel and hasattr(message.channel, "fetch_message"):
                    ref_msg = await message.channel.fetch_message(ref.message_id)
            except Exception:
                ref_msg = None
        if isinstance(ref_msg, discord.Message):
            url = _first_image_url_from_message(ref_msg)
            if url:
                return url

    # 3) Forwarded posts via message_snapshots (discord.py 2.4+)
    try:
        snaps = getattr(message, "message_snapshots", None) or []
        for snap in snaps:
            inner = getattr(snap, "message", None) or getattr(snap, "resolved", None) or snap
            url = _first_image_url_from_message(inner)
            if url:
                return url
    except Exception:
        pass

    # 4) As a last resort, try again scanning embeds only, in case of forwarded-style messages
    for e in getattr(message, "embeds", []) or []:
        try:
            if e.image and e.image.url:
                return str(e.image.url)
        except Exception:
            pass
        try:
            if e.thumbnail and e.thumbnail.url:
                return str(e.thumbnail.url)
        except Exception:
            pass

    return None


class ReverseImageCog(commands.Cog):
    """Reverse image search via message context menu."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._registered = False
        self._register_lock = asyncio.Lock()

    async def _ensure_registered(self) -> None:
        if self._registered:
            return
        async with self._register_lock:
            if self._registered:
                return

            if not _as_bool("REVERSE_IMAGE_ENABLE", True):
                self._registered = True
                return

            ctx_name = "Reverse image (Nixe)"

            # Remove any legacy command with the same name to avoid duplicates.
            try:
                existing = list(self.bot.tree.get_commands())
                for cmd in existing:
                    if isinstance(cmd, app_commands.ContextMenu) and cmd.name == ctx_name:
                        try:
                            self.bot.tree.remove_command(cmd.name, type=cmd.type)
                        except Exception:
                            pass
            except Exception:
                pass

            try:
                self.bot.tree.add_command(
                    app_commands.ContextMenu(name=ctx_name, callback=self.trace_image_ctx),
                )
            except Exception:
                # If registration fails, do not crash the bot; just skip.
                pass

            # Optionally sync the tree so the context menu appears in Discord UI.
            if _as_bool("REVERSE_IMAGE_SYNC_ON_BOOT", True):
                try:
                    await self.bot.tree.sync()
                except Exception:
                    # best-effort only; do not crash if sync fails
                    pass

            if _as_bool("REVERSE_IMAGE_SYNC_ON_BOOT", True):
                try:
                    await self.bot.tree.sync()
                except Exception:
                    # Avoid crashing; this is best-effort only.
                    pass

            self._registered = True

    async def trace_image_ctx(self, interaction: discord.Interaction, message: discord.Message):
        """Message context menu callback for reverse image search."""
        if not _as_bool("REVERSE_IMAGE_ENABLE", True):
            await interaction.response.send_message("Reverse image search is disabled.", ephemeral=True)
            return

        ephemeral = _as_bool("REVERSE_IMAGE_EPHEMERAL", True)
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)

        # Context menu may pass a partial Message; refetch for full attachments/embeds when possible.
        full_msg = message
        try:
            if interaction.channel and hasattr(interaction.channel, "fetch_message") and message.id:
                full_msg = await interaction.channel.fetch_message(message.id)
        except Exception:
            full_msg = message

        url = await _extract_image_url(full_msg)
        if not url:
            await interaction.followup.send(
                "Tidak ditemukan gambar di pesan ini ataupun pesan yang direply.",
                ephemeral=ephemeral,
            )
            return

        enc = quote_plus(str(url))

        google_lens = f"https://lens.google.com/uploadbyurl?url={enc}"
        bing = f"https://www.bing.com/images/search?view=detailv2&iss=sbi&imgurl={enc}"
        yandex = f"https://yandex.com/images/search?rpt=imageview&url={enc}"
        tineye = f"https://tineye.com/search?url={enc}"

        embed = discord.Embed(title="Reverse image search")
        embed.description = "Klik salah satu tautan di bawah untuk mencari sumber gambar ini."

        embed.add_field(name="Discord CDN", value=url, inline=False)
        embed.add_field(name="Google Lens", value=f"[Open]({google_lens})", inline=True)
        embed.add_field(name="Bing Visual Search", value=f"[Open]({bing})", inline=True)
        embed.add_field(name="Yandex", value=f"[Open]({yandex})", inline=True)
        embed.add_field(name="TinEye", value=f"[Open]({tineye})", inline=True)

        embed.set_footer(text="source=image • mode=reverse-search")

        await interaction.followup.send(embed=embed, ephemeral=ephemeral)

    @commands.Cog.listener()
    async def on_ready(self):
        # Delay registration until the bot is fully ready.
        if not self._registered:
            self.bot.loop.create_task(self._ensure_registered())


async def setup(bot: commands.Bot):
    cog = ReverseImageCog(bot)
    await bot.add_cog(cog)
