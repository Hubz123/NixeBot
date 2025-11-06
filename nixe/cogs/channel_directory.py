
# -*- coding: utf-8 -*-
import os
import re
import asyncio
import logging
from typing import List, Optional

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

TRIGGERS_DEFAULT = [
    "nixe channel list",
    "nixe list channel",
    "nixe channels",
    "channel list nixe",
]

def _env(key: str, default: str = "") -> str:
    v = os.getenv(key)
    return str(v) if v is not None else default

def _parse_ids(s: str) -> List[int]:
    out: List[int] = []
    if not s:
        return out
    s = s.replace(";", ",")
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except Exception:
            pass
    return out

def _mention(i: Optional[int]) -> str:
    try:
        if not i or int(i) <= 0:
            return "-"
        return f"<#{int(i)}>"
    except Exception:
        return "-"

def _join_mentions(ids: List[int]) -> str:
    if not ids:
        return "-"
    return ", ".join(_mention(i) for i in ids)

class ChannelDirectory(commands.Cog):
    """Plain-text channel directory: 'nixe channel list' (no prefix).
    Sends up to 3 messages and deletes them (and the trigger) after TTL seconds.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.triggers = self._load_triggers()
        self.ttl = max(1, int(_env("CHANLIST_TTL_SEC", "10")))
        self.cooldown = max(0, int(_env("CHANLIST_COOLDOWN_SEC", "3")))
        self._recent: set[int] = set()
        log.info("[chan-list] ready; ttl=%ss cooldown=%ss triggers=%s", self.ttl, self.cooldown, self.triggers)

    def _load_triggers(self) -> List[str]:
        raw = _env("CHANLIST_TRIGGERS", "")
        if raw.strip():
            arr = [x.strip().lower() for x in re.split(r"[,\n;]", raw) if x.strip()]
            if arr:
                return arr
        return [t.lower() for t in TRIGGERS_DEFAULT]

    # --- wiring helpers (read from env exported by runtime_env.json) ---
    def _wiring_lpg(self) -> str:
        guard = _parse_ids(_env("LPG_GUARD_CHANNELS", _env("LUCKYPULL_GUARD_CHANNELS","")))
        redirect = int(_env("LPG_REDIRECT_CHANNEL_ID", _env("LUCKYPULL_REDIRECT_CHANNEL_ID","0")) or 0)
        wl_thread = int(_env("LPG_WHITELIST_THREAD_ID", "0") or 0)
        neg_thread = int(_env("PHASH_IMAGEPHISH_THREAD_ID", "0") or 0)  # reuse inbox thread as FP box if set
        lines = [
            f"**Guard** : {_join_mentions(guard)}",
            f"**Redirect** : {_mention(redirect)}",
            f"**Whitelist (FP)** : {_mention(wl_thread)}",
            f"**Mirror Inbox** : {_mention(neg_thread)}",
        ]
        return "\n".join(lines)

    def _wiring_phish(self) -> str:
        db_parent = int(_env("PHASH_DB_PARENT_CHANNEL_ID","0") or 0)
        db_thread = int(_env("PHASH_DB_THREAD_ID","0") or 0)
        img_thread = int(_env("PHASH_IMAGEPHISH_THREAD_ID","0") or 0)
        log_chan = int(_env("NIXE_PHISH_LOG_CHAN_ID", _env("PHISH_LOG_CHAN_ID","0")) or 0)
        lines = [
            f"**DB Parent** : {_mention(db_parent)}",
            f"**DB Thread** : {_mention(db_thread)}",
            f"**Image Inbox** : {_mention(img_thread)}",
            f"**Phish Log** : {_mention(log_chan)}",
        ]
        return "\n".join(lines)

    def _wiring_misc(self) -> str:
        log_chan = int(_env("LOG_CHANNEL_ID","0") or 0)
        casino_scope = _parse_ids(_env("CRYPTO_CASINO_SCOPE",""))
        protect = [_env("PHASH_DB_THREAD_ID","0"), _env("PHASH_IMAGEPHISH_THREAD_ID","0")]
        protect = [int(x) for x in protect if str(x).isdigit()]
        lines = [
            f"**Log** : {_mention(log_chan)}",
            f"**Casino Scope** : {_join_mentions(casino_scope)}",
            f"**Protected** : {_join_mentions(protect)}",
        ]
        return "\n".join(lines)

    async def _delete_later(self, *msgs: discord.Message):
        await asyncio.sleep(self.ttl)
        for m in msgs:
            try:
                await m.delete()
            except Exception:
                pass

    def _match_trigger(self, content: str) -> bool:
        c = content.lower().strip()
        return any(c == t or c.startswith(t) for t in self.triggers)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # ignore bots & DMs
        if not message.guild or message.author.bot:
            return
        if not self._match_trigger(message.content):
            return
        # simple anti-spam per-channel
        if self.cooldown:
            if message.channel.id in self._recent:
                return
            self._recent.add(message.channel.id)
            async def _rm():
                await asyncio.sleep(self.cooldown)
                self._recent.discard(message.channel.id)
            asyncio.create_task(_rm())

        # build payloads
        try:
            p1 = discord.Embed(title="Nixe • Channel List (LPG)", description=self._wiring_lpg())
            p2 = discord.Embed(title="Nixe • Channel List (PHISHING)", description=self._wiring_phish(), color=0xE67E22)
            p3 = discord.Embed(title="Nixe • Channel List (MISC)", description=self._wiring_misc(), color=0x95A5A6)
            s1 = await message.channel.send(embed=p1)
            s2 = await message.channel.send(embed=p2)
            s3 = await message.channel.send(embed=p3)
            # schedule deletion of bot messages + trigger
            asyncio.create_task(self._delete_later(message, s1, s2, s3))
        except Exception as e:
            log.exception("channel list send failed: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelDirectory(bot))
