# -*- coding: utf-8 -*-
"""
nixe.cogs.a16_lpg_persona_react_overlay
Patch: fix persona send error (pick_line unexpected kwarg 'user') and
always mention redirect channel by ID from env (LPG_REDIRECT_CHANNEL_ID / LUCKYPULL_REDIRECT_CHANNEL_ID).
Does NOT modify yandere.json.
"""
from __future__ import annotations

import os
import re
import logging
import asyncio
from typing import Optional

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

# Optional import; adjust path if your project stores pick_line elsewhere.
try:
    from nixe.helpers.persona_text import pick_line  # noqa: F401
except Exception:
    pick_line = None  # type: ignore


def _get_redirect_channel_id() -> Optional[int]:
    """Resolve redirect channel ID from environment (supports both keys)."""
    for k in ("LPG_REDIRECT_CHANNEL_ID", "LUCKYPULL_REDIRECT_CHANNEL_ID"):
        v = os.getenv(k)
        if v and v.isdigit():
            return int(v)
    return None


def _safe_persona_line(tone: str, user_mention: str, redirect_mention: str) -> str:
    """
    Use yandere.json through pick_line() if available, but don't pass kwargs that
    older implementations don't accept. Then post-format placeholders if present.
    Finally, ensure any '#ngobrol' text is replaced with the actual redirect mention.
    """
    text = None
    if pick_line:
        try:
            # Old signature: pick_line(pool, tone=None)
            text = pick_line("yandere", tone)  # type: ignore[misc]
        except TypeError:
            # Some variants accept only (pool); fallback
            try:
                text = pick_line("yandere")  # type: ignore[misc]
            except Exception:
                text = None
        except Exception:
            text = None

    if not text:
        # Hard fallback phrase (won't be used if yandere pool works)
        text = "psst {user}… pamer yang ini di {channel} aja ya~"

    # Try .format placeholders if they exist
    try:
        text = text.format(user=user_mention, channel=redirect_mention)
    except Exception:
        pass

    # Replace any literal '#ngobrol' variants with redirect mention, without touching yandere.json
    # Covers: '#ngobrol', '#・ngobrol', '# ･ ngobrol', '# ngobrol', etc.
    text = re.sub(r"#\s*[^\s#]*ngobrol[^\s#]*", redirect_mention, text, flags=re.IGNORECASE)
    # Also replace plain 'ngobrol' if it's standing alone
    text = re.sub(r"\bngobrol\b", redirect_mention, text, flags=re.IGNORECASE)
    return text


class LPGPersonaReactOverlay(commands.Cog):
    """Send a short yandere persona nudge telling users to move Lucky Pulls to the redirect channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.notice_ttl = int(os.getenv("LPG_PERSONA_NOTICE_TTL", "25"))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        log.info("[lpg-persona] ready (ttl=%ss)", self.notice_ttl)

    async def send_persona_notice(self, channel: discord.TextChannel, member: discord.Member, *, tone: str = "soft") -> None:
        """Public helper: send the persona line to `channel` and auto-delete after TTL."""
        redir_id = _get_redirect_channel_id()
        if not redir_id:
            log.warning("[lpg-persona] redirect channel ID is not set; skip persona send")
            return

        # Try to resolve the channel for proper mention
        guild = channel.guild
        redir_chan = guild.get_channel(redir_id)
        if redir_chan is None:
            try:
                redir_chan = await self.bot.fetch_channel(redir_id)
            except Exception:
                redir_chan = None
        redirect_mention = getattr(redir_chan, "mention", f"<#{redir_id}>")

        # Build line via yandere.json without changing the file
        line = _safe_persona_line(tone=tone, user_mention=member.mention, redirect_mention=redirect_mention)

        try:
            await channel.send(line, delete_after=self.notice_ttl)
        except Exception as e:
            log.exception("[lpg-persona] send failed: %r", e)


async def setup(bot: commands.Bot) -> None:  # Pycord/nextcord compat: use setup_hook if needed
    await bot.add_cog(LPGPersonaReactOverlay(bot))
