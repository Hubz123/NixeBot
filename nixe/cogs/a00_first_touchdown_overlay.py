# -*- coding: utf-8 -*-
"""
First Touchdown overlay:
- Enforces FIRST_TOUCHDOWN_* env policy on phishing events and (if present) pHash match events.
- No config changes; purely reads runtime_env.json/.env.
"""
from __future__ import annotations
import os, logging, asyncio, contextlib
import discord
from discord.ext import commands
from nixe.helpers.safe_delete import safe_delete

log = logging.getLogger("nixe.cogs.a00_first_touchdown_overlay")

def _ids(val: str) -> set[int]:
    out=set()
    for tok in (val or "").replace(";",",").split(","):
        tok=tok.strip()
        if tok.isdigit(): out.add(int(tok))
    return out

class FirstTouchdown(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enable = (os.getenv("FIRST_TOUCHDOWN_ENABLE","0") == "1")
        self.chan = _ids(os.getenv("FIRST_TOUCHDOWN_CHANNELS",""))
        self.bypass = _ids(os.getenv("FIRST_TOUCHDOWN_BYPASS_CHANNELS","")) | _ids(os.getenv("PROTECT_CHANNEL_IDS",""))
        self.ban_on_phash = (os.getenv("FIRST_TOUCHDOWN_BAN_ON_PHASH","0") == "1")
        self.delete_days = int(os.getenv("PHISH_DELETE_MESSAGE_DAYS","0") or "0")
        log.info("[first-touchdown] enable=%s channels=%s bypass=%s ban_on_phash=%s", self.enable, sorted(self.chan), sorted(self.bypass), self.ban_on_phash)

    # Generic helper: try to ban + delete safely
    async def _ban_and_delete(self, guild: discord.Guild|None, channel: discord.abc.Messageable|None, user_id: int|None, message_id: int|None, reason: str):
        if not guild or not user_id: 
            return
        try:
            if channel and isinstance(channel, discord.TextChannel):
                if channel.id in self.bypass:
                    return  # never act in bypass/protect channels
            member = guild.get_member(user_id) or (await guild.fetch_member(user_id)) if user_id else None
            if member:
                with contextlib.suppress(Exception):
                    await guild.ban(member, reason=reason[:180], delete_message_days=self.delete_days if self.delete_days>=0 else 0)
            if channel and message_id and isinstance(channel, discord.TextChannel):
                if channel.id not in self.bypass:
                    with contextlib.suppress(Exception):
                        msg = await channel.fetch_message(int(message_id))
                        await safe_delete(msg, label="delete")
        except Exception:
            pass

    # Hook primary phishing event (already emitted by groq/link guard)
    @commands.Cog.listener("on_nixe_phish_detected")
    async def on_nixe_phish_detected(self, payload: dict):
        if not self.enable: return
        try:
            cid = int(payload.get("channel_id") or 0)
            gid = int(payload.get("guild_id") or 0)
            uid = int(payload.get("user_id") or 0)
            mid = int(payload.get("message_id") or 0)
            if cid and (cid in self.chan) and (cid not in self.bypass):
                guild = self.bot.get_guild(gid) if gid else None
                channel = self.bot.get_channel(cid) if cid else None
                await self._ban_and_delete(guild, channel, uid, mid, "FirstTouchdown: phishing detected")
        except Exception:
            pass

    # Hook optional pHash events if present
    @commands.Cog.listener("on_nixe_phash_match")
    async def on_nixe_phash_match(self, payload: dict):
        if not (self.enable and self.ban_on_phash): return
        try:
            cid = int(payload.get("channel_id") or 0)
            gid = int(payload.get("guild_id") or 0)
            uid = int(payload.get("user_id") or 0)
            mid = int(payload.get("message_id") or 0)
            if cid and (cid in self.chan) and (cid not in self.bypass):
                guild = self.bot.get_guild(gid) if gid else None
                channel = self.bot.get_channel(cid) if cid else None
                await self._ban_and_delete(guild, channel, uid, mid, "FirstTouchdown: pHash match")
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(FirstTouchdown(bot))
