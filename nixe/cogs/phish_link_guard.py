# -*- coding: utf-8 -*-
"""
Fast link-phishing guard (no model call).
Use-case: messages that only contain multiple links like
  https://i.ibb.co/.../image.png
Often those serve WEBP/other payloads and are classic bait.
This guard:
- scans message content for URLs,
- matches suspicious hosts & filenames,
- emits the same "nixe_phish_detected" event used by ban embed.
It respects existing env flags; no new config is required.
"""
from __future__ import annotations
import os, re, logging, asyncio
import discord
from discord.ext import commands
from nixe.helpers.ban_utils import emit_phish_detected

log = logging.getLogger("nixe.cogs.phish_link_guard")

URL_RE = re.compile(r'https?://[^\s<>()]+' , re.I)
# Default suspicious hosts; can be extended via PHISH_LINK_HOSTS (comma-separated)
DEFAULT_HOSTS = {"i.ibb.co", "ibb.co", "postimg.cc", "postimg.cc", "imgbb.com", "tinypic.com", "imgur.com", "pinimg.com"}
# Filenames that are commonly abused
SUS_FILENAMES = {"image.png", "img.png", "photo.png", "image.jpg", "img.jpg"}
GUARD_ALL = (os.getenv("PHISH_GUARD_ALL_CHANNELS","1").strip().lower() in ("1","true","yes","on"))


def _host(url: str) -> str:
    try:
        m = re.match(r'https?://([^/]+)', url, re.I)
        return (m.group(1) or "").lower().strip() if m else ""
    except Exception:
        return ""

def _name(url: str) -> str:
    try:
        tail = url.split("?")[0].rstrip("/").split("/")[-1]
        return tail.lower()
    except Exception:
        return ""

class LinkPhishGuard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enable = (os.getenv("PHISH_LINK_ENABLE","1") == "1")
        self.guard_ids = set(int(x) for x in (os.getenv("LPG_GUARD_CHANNELS","") or "").replace(";",",").split(",") if x.strip().isdigit())
        skip_raw = (os.getenv("PHISH_SKIP_CHANNELS","") or "")
        self.skip_ids = set(int(x) for x in skip_raw.replace(";",",").split(",") if x.strip().isdigit())
        if not self.skip_ids:
            # Default: mod channels excluded from link-phish guard
            self.skip_ids = {1400375184048787566, 936690788946030613}
        hosts_env = os.getenv("PHISH_LINK_HOSTS","")
        self.hosts = DEFAULT_HOSTS | {h.strip().lower() for h in hosts_env.split(",") if h.strip()}
        self.min_links = int(os.getenv("PHISH_LINK_MIN_COUNT","2"))
        log.info("[phish-link] enable=%s guards=%s hosts=%s skip=%s", self.enable, sorted(self.guard_ids), sorted(self.hosts), sorted(self.skip_ids))

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if not self.enable: return
        try:
            if message.author.bot: return
            ch = getattr(message,"channel",None)
            if not ch: return
            cid = int(getattr(ch,"id",0) or 0)
            pid = int(getattr(ch,"parent_id",0) or 0)
            if pid and not GUARD_ALL:
                return
            if (cid in self.skip_ids) or (pid and pid in self.skip_ids):
                return
            if (not GUARD_ALL) and not ((cid in self.guard_ids) or (pid and pid in self.guard_ids)):
                return

            text = (message.content or "") + " " + " ".join(a.url for a in getattr(message,"attachments",[]) or [])
            urls = URL_RE.findall(text)
            if not urls: 
                return

            sus = []
            for u in urls:
                h = _host(u)
                n = _name(u)
                if h in self.hosts and (n in SUS_FILENAMES or n.endswith(".png") or n.endswith(".jpg")):
                    sus.append(u)

            # Rule: if there are >= min_links suspicious links, treat as phishing immediately
            if len(sus) >= self.min_links:
                reason = f"suspicious links ({len(sus)}): " + ", ".join(sus[:4])
                emit_phish_detected(self.bot, message, {"score": 1.0, "provider": "link-guard", "reason": reason, "kind": "links"}, sus[:4])
                log.warning("[phish-link] detected -> %s", reason)
        except Exception as e:
            log.debug("[phish-link] err: %r", e)


    @commands.Cog.listener("on_message_edit")
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Re-run link phishing checks on edited messages so users cannot
        # bypass the guard by editing an old message into a link blast.
        if not self.enable:
            return
        try:
            await self.on_message(after)
        except Exception as e:
            log.debug("[phish-link] edit err: %r", e)


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkPhishGuard(bot))
