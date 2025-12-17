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
LOG_CH_ID = _to_int(os.getenv("LOG_CHANNEL_ID", "0"), 0)
BAN_LOG_CH_ID = _to_int(os.getenv("NIXE_BAN_LOG_CHANNEL_ID", str(LOG_CH_ID)), 0)

# Optional block (never use this channel as a target)
BLOCKED_ID = _to_int(os.getenv("LOG_CHANNEL_BLOCKED_ID", "0"), 0)

PREF_NAME = (os.getenv("MOD_LOG_CHANNEL_NAME", "nixe-only") or "nixe-only").strip().lower()

def _ok(ch: Optional[discord.abc.GuildChannel]) -> Optional[discord.TextChannel]:
    if not ch:
        return None
    if getattr(ch, "id", 0) and int(getattr(ch, "id", 0)) == int(BLOCKED_ID):
        return None
    if isinstance(ch, discord.TextChannel):
        return ch
    return None

async def get_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Resolve a safe log channel for this guild."""
    if not guild:
        return None

    # 1) Explicit ban-log channel id
    if BAN_LOG_CH_ID:
        ch = _ok(guild.get_channel(BAN_LOG_CH_ID))
        if ch:
            return ch

    # 2) Explicit LOG_CHANNEL_ID
    if LOG_CH_ID:
        ch = _ok(guild.get_channel(LOG_CH_ID))
        if ch:
            return ch

    # 3) Name-based fallback (nixe-only)
    try:
        for c in guild.text_channels:
            if (c.name or "").strip().lower() == PREF_NAME and int(c.id) != int(BLOCKED_ID):
                return c
    except Exception:
        pass

    return None

async def get_ban_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    return await get_log_channel(guild)

async def ensure_ban_thread(ch: discord.TextChannel):
    """Compatibility stub (thread logging not used in this build)."""
    return None
