# -*- coding: utf-8 -*-
"""
nixe.cogs.gacha_luck_guard  — env‑driven redirect
"""
from __future__ import annotations

import os, re, logging
from typing import Optional, List, Set
import discord
from discord.ext import commands

log = logging.getLogger(__name__)

def _env_str(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

def _env_int(*keys: str) -> Optional[int]:
    for k in keys:
        v = os.getenv(k)
        if v and v.isdigit():
            try: return int(v)
            except Exception: pass
    return None

def _env_bool(*pairs, default=False) -> bool:
    for k, d in pairs:
        v = os.getenv(k, d)
        s = str(v).strip().lower() if v is not None else ""
        if s in ("1","true","yes","on"): return True
        if s in ("0","false","no","off"): return False
    return default

def _parse_ids(v: str) -> Set[int]:
    return {int(tok) for tok in (v or '').replace(' ','').split(',') if tok.isdigit()}

def _ttl() -> int:
    v = _env_str("LPG_PERSONA_NOTICE_TTL", "LPA_PERSONA_NOTICE_TTL", default="10")
    try: return max(3, int(v))
    except Exception: return 10

_TOKEN = re.compile(r"(?:<#\d+>|#\s*[^\s#]*ngobrol[^\s#]*|\bngobrol\b)", re.IGNORECASE)

class GachaLuckGuard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enable = _env_bool(("GACHA_GUARD_ENABLE","1"), default=True)
        guards = _env_str("LUCKYPULL_GUARD_CHANNELS", "LPG_GUARD_CHANNELS", default="")
        self.guard_channels = _parse_ids(guards)
        self.redirect_id = _env_int("LUCKYPULL_REDIRECT_CHANNEL_ID") or _env_int("LPG_REDIRECT_CHANNEL_ID") or _env_int("LPA_REDIRECT_CHANNEL_ID")
        self.mention = _env_bool(("LUCKYPULL_MENTION_USER","1"), ("LPG_MENTION","1"), default=True)
        self.delete_src = _env_bool(("LUCKYPULL_DELETE_ON_GUARD","1"), default=True)
        self._ttl = _ttl()
        log.warning("[gacha-guard] ready guards=%s redirect=%s ttl=%ss", sorted(self.guard_channels), self.redirect_id, self._ttl)

    def _is_guard(self, ch: discord.abc.GuildChannel) -> bool:
        try: return int(ch.id) in self.guard_channels
        except Exception: return False

    async def _redir_mention(self, guild: Optional[discord.Guild]) -> str:
        rid = self.redirect_id
        if not rid: return "#unknown"
        ch = guild.get_channel(rid) if guild else None
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(rid)
            except Exception:
                ch = None
        return getattr(ch, "mention", f"<#{rid}>")

    async def _notify(self, message: discord.Message):
        redir = await self._redir_mention(message.guild)
        user = message.author.mention if self.mention else str(message.author)
        text = f"psst {user}… lanjutkan di {redir} ya, biar rapi 💖"
        text = _TOKEN.sub(redir, text)
        try:
            await message.channel.send(text, delete_after=self._ttl)
        except Exception as e:
            log.warning("[gacha-guard] notify failed: %r", e)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.enable or message.author.bot: return
        if not isinstance(message.channel, discord.TextChannel): return
        if not self._is_guard(message.channel): return
        has_img = any((a.content_type or "").startswith("image/") for a in message.attachments)
        if not has_img: return
        await self._notify(message)
        if self.redirect_id and self.delete_src:
            try: await message.delete()
            except Exception: pass

async def setup(bot: commands.Bot):
    res = bot.add_cog(GachaLuckGuard(bot))
    try:
        import asyncio
        if asyncio.iscoroutine(res): await res
    except Exception:
        pass
