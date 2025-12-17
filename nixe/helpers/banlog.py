# -*- coding: utf-8 -*-
"""nixe.helpers.banlog

Centralized resolution for the bot's moderation / phishing log channel.

Key points:
- LOG_CHANNEL_ID / NIXE_BAN_LOG_CHANNEL_ID select the primary log channel.
- LOG_CHANNEL_BLOCKED_ID can optionally block a specific channel ID from being used.
  (Important: do NOT block LOG_CHANNEL_ID by default.)
"""

from __future__ import annotations

import os
import logging
from typing import Optional

import discord

__all__ = ["get_log_channel", "get_ban_log_channel", "ensure_ban_thread"]

log = logging.getLogger(__name__)

def _to_int(v: str, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return int(default)

# Primary log channel config
# NOTE: resolve environment at call-time to avoid import-order issues.
def _env_first(*names: str, default: str = "") -> str:
    for n in names:
        try:
            v = os.getenv(n)
        except Exception:
            v = None
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return default

def _cfg():
    # Primary log channel id: accept legacy and newer env names.
    log_id = _to_int(_env_first(
        "PHISH_LOG_CHAN_ID",
        "NIXE_PHISH_LOG_CHAN_ID",
        "LOG_CHANNEL_ID",
        "NIXE_LOG_CHANNEL_ID",
        default="0",
    ), 0)

    # Ban/mod log channel id: prefer explicit ban-log id, otherwise fall back to log_id.
    ban_log_id = _to_int(_env_first(
        "NIXE_BAN_LOG_CHANNEL_ID",
        "BAN_LOG_CHANNEL_ID",
        default=str(log_id or 0),
    ), 0)

    blocked_id = _to_int(_env_first(
        "LOG_CHANNEL_BLOCKED_ID",
        "PHISH_LOG_BLOCKED_ID",
        "NIXE_LOG_CHANNEL_BLOCKED_ID",
        default="0",
    ), 0)

    pref_name = (_env_first(
        "MOD_LOG_CHANNEL_NAME",
        "NIXE_LOG_CHANNEL_NAME",
        default="nixe-only",
    ) or "nixe-only").strip().lower()

    return log_id, ban_log_id, blocked_id, pref_name

def _ok(ch: Optional[discord.abc.GuildChannel], blocked_id: int) -> Optional[discord.TextChannel]:
    if not ch:
        return None
    if getattr(ch, "id", 0) and int(getattr(ch, "id", 0)) == int(blocked_id):
        return None
    if isinstance(ch, discord.TextChannel):
        return ch
    return None

async def get_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Resolve a safe log channel for this guild."""
    if not guild:
        return None

    log_id, ban_log_id, blocked_id, pref_name = _cfg()

    # 1) Explicit ban-log channel id
    if ban_log_id:
        ch = _ok(guild.get_channel(ban_log_id), blocked_id)
        if ch:
            return ch

    # 2) Explicit LOG/PHISH log channel id
    if log_id:
        ch = _ok(guild.get_channel(log_id), blocked_id)
        if ch:
            return ch

    # 3) Name-based fallback
    try:
        for c in guild.text_channels:
            if (c.name or "").strip().lower() == pref_name and int(c.id) != int(blocked_id):
                return c
    except Exception:
        pass

    return None

async def get_ban_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    return await get_log_channel(guild)

async def ensure_ban_thread(ch: discord.TextChannel):
    """Compatibility stub (thread logging not used in this build)."""
    return None
