# -*- coding: utf-8 -*-
"""
nixe.cogs.a16_lpa_persona_redirect_filter_overlay

Purpose:
- Some cogs (e.g., lucky_pull_auto) send persona text with hard-coded '#ngobrol'.
- This overlay post-filters bot messages in guard channels and edits the content
  to force the correct redirect mention from ENV, without changing yandere.json
  or other cogs.

ENV supported:
- LPG_GUARD_CHANNELS / LUCKYPULL_GUARD_CHANNELS  (comma-separated IDs)
- LPG_REDIRECT_CHANNEL_ID / LUCKYPULL_REDIRECT_CHANNEL_ID / LPA_REDIRECT_CHANNEL_ID

This overlay is intentionally NOT named '...lpg_persona_react_overlay' to avoid being
unloaded by 'a00_block_duplicate_persona_overlay'.
"""
from __future__ import annotations

import os
import re
import logging
from typing import Optional, Set, List

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

_FORCE_CH_PATTERN = re.compile(
    r"(?:"
    r"<#\d+>"                                 # any channel mention e.g. <#123>
    r"|#\s*[^\s#]*ngobrol[^\s#]*"             # '#・ngobrol' (various separators)
    r"|\bngobrol\b"                            # plain word 'ngobrol'
    r")",
    flags=re.IGNORECASE,
)

def _split_ids(env_key: str) -> List[int]:
    raw = os.getenv(env_key) or ""
    out: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out

def _guard_channels() -> Set[int]:
    ids = set(_split_ids("LPG_GUARD_CHANNELS"))
    if not ids:
        ids = set(_split_ids("LUCKYPULL_GUARD_CHANNELS"))
    return ids

def _redirect_channel_id() -> Optional[int]:
    for k in ("LPG_REDIRECT_CHANNEL_ID", "LUCKYPULL_REDIRECT_CHANNEL_ID", "LPA_REDIRECT_CHANNEL_ID"):
        v = os.getenv(k)
        if v and v.isdigit():
            return int(v)
    return None


class LPAPersonaRedirectFilter(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._guards = _guard_channels()
        self._redir_id = _redirect_channel_id()
        log.info("[lpa-redirect-filter] guards=%s redirect=%s", sorted(self._guards), self._redir_id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # refresh env on ready in case env-hybrid loads after
        self._guards = _guard_channels()
        self._redir_id = _redirect_channel_id()
        log.info("[lpa-redirect-filter] ready guards=%s redirect=%s", sorted(self._guards), self._redir_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._redir_id or not self._guards:
            return
        if message.author.id != self.bot.user.id:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if message.channel.id not in self._guards:
            return
        # Only touch messages which include any token we want to replace
        content = message.content or ""
        if not _FORCE_CH_PATTERN.search(content):
            return

        guild = message.guild
        redir_chan = guild.get_channel(self._redir_id) if guild else None
        if redir_chan is None:
            try:
                redir_chan = await self.bot.fetch_channel(self._redir_id)
            except Exception:
                redir_chan = None
        redirect_mention = getattr(redir_chan, "mention", f"<#{self._redir_id}>")

        new_content, n = _FORCE_CH_PATTERN.subn(redirect_mention, content)
        if n == 0 or new_content == content:
            return
        try:
            await message.edit(content=new_content)
            log.info("[lpa-redirect-filter] fixed persona text in #%s -> %s", message.channel.id, redirect_mention)
        except Exception as e:
            log.exception("[lpa-redirect-filter] edit failed: %r", e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LPAPersonaRedirectFilter(bot))
