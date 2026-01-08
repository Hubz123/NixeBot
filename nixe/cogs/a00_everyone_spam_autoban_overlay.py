# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

import discord
from discord.ext import commands

from nixe.helpers.safe_delete import safe_delete

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _env_int(name: str, default: int) -> int:
    try:
        v = str(os.getenv(name, str(default))).strip()
        return int(float(v))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    v = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _parse_csv_ids(s: str) -> set[int]:
    out: set[int] = set()
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except Exception:
            continue
    return out


class EveryoneSpamAutoban(commands.Cog):
    """Ban users who spam @everyone/@here across channels (fast containment).

    Design goals:
    - Containment must be fast: delete first, then ban once spam pattern is confirmed.
    - Avoid false positives: exempt privileged roles/perms; require multi-channel spam unless URL/attachment is present.
    - No external dependencies.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # guild_id -> user_id -> deque[(ts, channel_id)]
        self._hist: Dict[int, Dict[int, Deque[Tuple[float, int]]]] = defaultdict(lambda: defaultdict(deque))
        # (guild_id,user_id) -> ts when banned/acted
        self._cooldown: Dict[Tuple[int, int], float] = {}

    def _should_exempt(self, member: discord.Member) -> bool:
        # Exempt by permission (admins/mods)
        perms = getattr(member, "guild_permissions", None)
        if perms:
            if perms.administrator or perms.manage_guild or perms.manage_messages or perms.ban_members:
                return True

        # Exempt by role ids (config)
        ex_role_ids = _parse_csv_ids(os.getenv("PHISH_EVERYONE_SPAM_EXEMPT_ROLE_IDS", ""))
        if ex_role_ids:
            try:
                for r in member.roles:
                    if int(r.id) in ex_role_ids:
                        return True
            except Exception:
                pass
        return False

    async def _ban_member(self, guild: discord.Guild, member: discord.Member, *, reason: str) -> bool:
        # Purge based on PHISH_DELETE_MESSAGE_DAYS (clamped to 0..7)
        purge_days = _env_int("PHISH_DELETE_MESSAGE_DAYS", 0)
        purge_days = max(0, min(7, purge_days))
        purge_seconds = purge_days * 86400

        try:
            # discord.py 2.3+ supports delete_message_seconds
            if purge_seconds > 0:
                await guild.ban(member, reason=reason[:480], delete_message_seconds=purge_seconds)
            else:
                await guild.ban(member, reason=reason[:480], delete_message_days=0)
            return True
        except TypeError:
            # Older discord.py
            try:
                await guild.ban(member, reason=reason[:480], delete_message_days=purge_days)
                return True
            except Exception as e:
                log.warning("[everyone-spam] ban failed (fallback): %r", e)
                return False
        except Exception as e:
            log.warning("[everyone-spam] ban failed: %r", e)
            return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            if not message or not getattr(message, "guild", None):
                return
            if getattr(message.author, "bot", False):
                return

            enable = _env_bool("PHISH_EVERYONE_SPAM_BAN_ENABLE", True)
            if not enable:
                return

            # Fast check: @everyone/@here
            if not getattr(message, "mention_everyone", False) and "@everyone" not in (message.content or "") and "@here" not in (message.content or ""):
                return

            guild: discord.Guild = message.guild
            member = guild.get_member(getattr(message.author, "id", 0)) or message.author
            if not isinstance(member, discord.Member):
                return

            if self._should_exempt(member):
                return

            # Best-effort delete immediately to contain spread.
            try:
                await safe_delete(message, label="[everyone-spam]", delay=0.0, reason="Mention-everyone spam containment")
            except Exception:
                pass

            uid = int(member.id)
            gid = int(guild.id)
            key = (gid, uid)

            # Cooldown: avoid repeated ban attempts/log spam.
            now = time.monotonic()
            cd_until = self._cooldown.get(key, 0.0)
            if cd_until and now < cd_until:
                return

            window = float(_env_int("PHISH_EVERYONE_SPAM_WINDOW_SEC", 45))
            min_channels = max(1, _env_int("PHISH_EVERYONE_SPAM_MIN_CHANNELS", 2))
            min_msgs = max(1, _env_int("PHISH_EVERYONE_SPAM_MIN_MSGS", 2))
            ban_on_first_with_url = _env_bool("PHISH_EVERYONE_SPAM_BAN_ON_FIRST_WITH_URL", True)

            # Evidence signals
            has_url = bool(_URL_RE.search(message.content or ""))
            has_attachment = bool(getattr(message, "attachments", None)) and len(message.attachments) > 0

            # Record
            dq = self._hist[gid][uid]
            dq.append((now, int(message.channel.id)))
            # Prune
            cutoff = now - window
            while dq and dq[0][0] < cutoff:
                dq.popleft()

            distinct_channels = len({ch for _, ch in dq})
            total_msgs = len(dq)

            ban_reason = "Mention-everyone spam across channels (fast containment)"

            ban_now = False
            if ban_on_first_with_url and (has_url or has_attachment):
                # Strong phishing/spam indicator: @everyone + url/attachment
                ban_now = True
                ban_reason = "Mention-everyone spam with URL/attachment"
            elif distinct_channels >= min_channels and total_msgs >= min_msgs:
                ban_now = True

            if not ban_now:
                # Not confirmed spam yet; keep tracking.
                return

            ok = await self._ban_member(guild, member, reason=ban_reason)
            # Cooldown for subsequent attempts
            self._cooldown[key] = now + 120.0
            if ok:
                log.warning("[everyone-spam] banned user=%s(%s) guild=%s ch=%s distinct_ch=%d msgs=%d url=%s att=%s",
                            getattr(member, "name", "unknown"), uid, getattr(guild, "name", gid),
                            getattr(message.channel, "id", "unknown"),
                            distinct_channels, total_msgs, has_url, has_attachment)
        except Exception as e:
            log.debug("[everyone-spam] err: %r", e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EveryoneSpamAutoban(bot))
