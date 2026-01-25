# -*- coding: utf-8 -*-
"""
nixe/cogs/a05_autorole_anywhere_overlay.py

Auto-assign a role when a member sends a message in a mapped channel.

Config: nixe/config/auto_role_anywhere.json

Schema (subset compatible with SatpamLeinac):
{
  "text_channels": {"<channel_id>": <role_id>},
  "threads": {"<thread_id>": <role_id>},
  "restrict_to_role_ids": [<role_id>, ...],
  "thread_join_cooldown_s": 3600,
  "grant_on_thread_member_join": false
}
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import discord
from discord.ext import commands

LOG = logging.getLogger(__name__)

DEFAULT_CFG = {
    "text_channels": {},
    "threads": {},
    "restrict_to_role_ids": [],
    "thread_join_cooldown_s": 3600,
    "grant_on_thread_member_join": False,
}

def _repo_root() -> Path:
    # .../nixe/cogs -> project root is 2 levels up
    return Path(__file__).resolve().parents[2]

CFG_PATH = _repo_root() / "nixe" / "config" / "auto_role_anywhere.json"

def _to_int_map(d: object) -> Dict[int, int]:
    out: Dict[int, int] = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        try:
            ki = int(k)
            if isinstance(v, dict):
                vi = int(v.get("role"))
            else:
                vi = int(v)
            out[ki] = vi
        except Exception:
            continue
    return out

class AutoRoleAnywhereOverlay(commands.Cog):
    """
    Assign a role when a user chats in specific channel/thread IDs.

    Notes:
    - This is NOT "autorole on join". It is "autorole on first chat in a mapped location".
    - Safe-by-default: if config missing/empty, does nothing.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cfg: Dict = dict(DEFAULT_CFG)
        self.text_map: Dict[int, int] = {}
        self.thread_map: Dict[int, int] = {}
        self.allowed_roles: Set[int] = set()
        # Reduce spam if the bot lacks permissions/hierarchy
        self._deny_until: Dict[Tuple[int, int], float] = {}  # (guild_id, role_id) -> epoch
        # Avoid repeated fetch/attempt loops on the same user
        self._recent_attempt: Dict[Tuple[int, int, int], float] = defaultdict(lambda: 0.0)  # (guild, user, role) -> epoch
        self._load_cfg()

    def _load_cfg(self) -> None:
        obj = {}
        try:
            obj = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        except Exception:
            obj = {}
        cfg = {**DEFAULT_CFG, **(obj or {})}
        self.cfg = cfg
        self.text_map = _to_int_map(cfg.get("text_channels", {})) or _to_int_map(cfg.get("channel_id_map", {}))
        self.thread_map = _to_int_map(cfg.get("threads", {})) or _to_int_map(cfg.get("thread_id_map", {}))
        mapped_roles = set(self.text_map.values()) | set(self.thread_map.values())

        extra = set()
        try:
            extra = set(int(x) for x in (cfg.get("restrict_to_role_ids") or []) if str(x).isdigit())
        except Exception:
            extra = set()

        self.allowed_roles = (mapped_roles if not extra else (mapped_roles & extra)) or mapped_roles

        if self.text_map or self.thread_map:
            LOG.info(
                "[autorole-anywhere] enabled: text=%d thread=%d roles=%d cfg=%s",
                len(self.text_map), len(self.thread_map), len(self.allowed_roles), str(CFG_PATH),
            )
        else:
            LOG.info("[autorole-anywhere] disabled (empty config): %s", str(CFG_PATH))

    def _mapped_role_for(self, channel: discord.abc.Messageable) -> Optional[int]:
        try:
            cid = int(getattr(channel, "id", 0) or 0)
        except Exception:
            return None
        if cid in self.text_map:
            return self.text_map[cid]
        if cid in self.thread_map:
            return self.thread_map[cid]
        return None

    async def _resolve_member(self, message: discord.Message) -> Optional[discord.Member]:
        # message.author is usually Member for guild messages
        author = getattr(message, "author", None)
        if isinstance(author, discord.Member):
            return author
        try:
            g = message.guild
            if not g:
                return None
            mid = int(getattr(author, "id", 0) or 0)
            if not mid:
                return None
            m = g.get_member(mid)
            if m:
                return m
            # last resort fetch (can be rate-limited; we only do it when needed)
            return await g.fetch_member(mid)
        except Exception:
            return None

    def _deny_check(self, guild_id: int, role_id: int) -> bool:
        until = self._deny_until.get((guild_id, role_id), 0.0)
        return time.time() < until

    def _deny_set(self, guild_id: int, role_id: int, seconds: int = 600) -> None:
        self._deny_until[(guild_id, role_id)] = time.time() + float(seconds)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Ignore DMs/system/bots
        try:
            if not getattr(message, "guild", None):
                return
            if getattr(getattr(message, "author", None), "bot", False):
                return
        except Exception:
            return

        role_id = self._mapped_role_for(message.channel)
        if not role_id:
            return
        if role_id not in self.allowed_roles:
            return

        guild = message.guild
        if not guild:
            return

        # Avoid repeated attempts for the same user if they spam
        uid = int(getattr(getattr(message, "author", None), "id", 0) or 0)
        if uid:
            key = (int(guild.id), uid, int(role_id))
            last = self._recent_attempt[key]
            if time.time() - last < 30.0:
                return
            self._recent_attempt[key] = time.time()

        if self._deny_check(int(guild.id), int(role_id)):
            return

        member = await self._resolve_member(message)
        if not member:
            return

        role = guild.get_role(int(role_id))
        if not role:
            return

        # Already has role
        try:
            if role in getattr(member, "roles", []):
                return
        except Exception:
            pass

        try:
            await member.add_roles(role, reason="autorole-anywhere: chatted in mapped channel")
        except discord.Forbidden:
            # hierarchy/permissions issue; suppress repeated spam
            LOG.warning(
                "[autorole-anywhere] Forbidden: reflect role hierarchy/Manage Roles. guild=%s role=%s channel=%s",
                getattr(guild, "id", "?"), role_id, getattr(message.channel, "id", "?"),
            )
            self._deny_set(int(guild.id), int(role_id), seconds=1800)
        except discord.HTTPException as e:
            # transient or rate limit; backoff briefly
            LOG.warning("[autorole-anywhere] HTTPException add_roles: %r", e)
            self._deny_set(int(guild.id), int(role_id), seconds=120)
        except Exception as e:
            LOG.warning("[autorole-anywhere] add_roles error: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoRoleAnywhereOverlay(bot))
