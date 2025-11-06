
"""
Channel Directory (natural trigger) with full TTL for all messages.

- Natural trigger: "nixe channel list" (no "!")
- Reads config from runtime_env.json (env overlay) and JSON layout file:
  CHANNEL_DIR_JSON_PATH -> default: nixe/config/channel_directory.json
- Auto delete ALL messages after CHANNEL_DIR_AUTO_DELETE_SEC (default 10s)
- Keeps optional ping to user via CHANNEL_DIR_PING_ON_HELP=1
"""

from __future__ import annotations
import os
import json
import re
import asyncio
import logging
from pathlib import Path
from typing import List, Tuple, Optional

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

def _get_env_bool(name: str, default: str = "0") -> bool:
    v = os.getenv(name, default)
    return str(v).strip() in ("1", "true", "True", "yes", "on")

def _get_env_int(name: str, default: str = "0") -> int:
    try:
        return int(float(str(os.getenv(name, default)).strip()))
    except Exception:
        return int(default)

def _color_from_any(v: Optional[str]) -> discord.Color:
    if not v:
        return discord.Color.blurple()
    s = str(v).strip()
    try:
        if s.startswith("#"):
            return discord.Color(int(s[1:], 16))
        if s.startswith("0x"):
            return discord.Color(int(s, 16))
        return discord.Color(int(s))
    except Exception:
        return discord.Color.blurple()

class ChannelDirectory(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Runtime env
        self.json_path = os.getenv("CHANNEL_DIR_JSON_PATH", "nixe/config/channel_directory.json")
        self.auto_delete = _get_env_int("CHANNEL_DIR_AUTO_DELETE_SEC", "10")
        self.ping_on_help = _get_env_bool("CHANNEL_DIR_PING_ON_HELP", "0")
        self.compact = _get_env_bool("CHANNEL_DIR_COMPACT", "1")
        # In case user prefers inline JSON via env
        self.inline_json = os.getenv("CHANNEL_DIR_ITEMS_JSON", "")

        # Pre-load config
        try:
            self.config = self._load_config()
            log.info("[channel-dir] loaded config from %s (sections=%d)",
                     self.json_path, len(self.config.get("sections", [])))
        except Exception as e:
            log.exception("[channel-dir] failed to load config: %s", e)
            self.config = {"title": "Channel Directory", "sections": []}

    # ---------- helpers ----------
    def _load_config(self) -> dict:
        # 1) Try file
        p = Path(self.json_path)
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)
        # 2) Try inline JSON
        if self.inline_json:
            return json.loads(self.inline_json)
        # 3) Fallback skeleton
        return {"title": "Channel Directory", "sections": []}

    def _match_natural_command(self, content: str) -> bool:
        # Accept: "nixe channel list" / "nixe @channel list" / "nixe channels list"
        c = content.lower().strip()
        if "nixe" not in c:
            return False
        return bool(re.search(r"\bnixe\b.*\bchannel[s]?\s+list\b", c))

    def _build_embeds(self, guild: Optional[discord.Guild]) -> List[Tuple[discord.Embed, Optional[discord.ui.View]]]:
        cfg = self.config or {}
        title = cfg.get("title") or "Channel Directory"
        color = _color_from_any(cfg.get("color"))
        footer = cfg.get("footer") or ""
        sections = cfg.get("sections") or []

        items: List[Tuple[discord.Embed, Optional[discord.ui.View]]] = []

        for sec in sections:
            sec_title = str(sec.get("title") or "").strip() or title
            embed = discord.Embed(title=sec_title, color=color)
            desc_lines = []
            for it in sec.get("items", []):
                # Prefer channel mention by id if available
                mention = None
                if "id" in it and guild:
                    try:
                        ch = guild.get_channel(int(it["id"])) or guild.get_thread(int(it["id"]))
                        if isinstance(ch, (discord.TextChannel, discord.Thread, discord.VoiceChannel, discord.StageChannel, discord.CategoryChannel)):
                            mention = ch.mention
                    except Exception:
                        pass
                mention = mention or it.get("mention") or it.get("name") or "—"
                note = it.get("note") or it.get("desc") or ""
                if note:
                    desc_lines.append(f"• {mention} — {note}")
                else:
                    desc_lines.append(f"• {mention}")
            if desc_lines:
                embed.description = "\n".join(desc_lines)
            if footer:
                embed.set_footer(text=footer)
            items.append((embed, None))
        # If no sections, send one fallback embed
        if not items:
            embed = discord.Embed(title=title, description="(no items configured)", color=color)
            if footer:
                embed.set_footer(text=footer)
            items.append((embed, None))
        return items

    async def _send_embeds(self, destination: discord.abc.Messageable, author: Optional[discord.Member] = None):
        items = self._build_embeds(getattr(destination, "guild", None))
        ttl = float(self.auto_delete) if self.auto_delete and self.auto_delete > 0 else None

        first_content = None
        if self.ping_on_help and author is not None:
            first_content = author.mention

        first = True
        sent_msgs: List[discord.Message] = []
        for em, view in items:
            # Apply delete_after to all messages if ttl set
            kwargs = {"embed": em}
            if view is not None:
                kwargs["view"] = view
            if first and first_content:
                kwargs["content"] = first_content
            if ttl:
                kwargs["delete_after"] = ttl
            msg = await destination.send(**kwargs)
            sent_msgs.append(msg)
            first = False

        # Safety net for old behavior when ttl missing: delete first only
        if not ttl and self.auto_delete > 0 and sent_msgs:
            try:
                await asyncio.sleep(self.auto_delete)
                await sent_msgs[0].delete()
            except Exception:
                pass

    # ---------- listeners & optional command ----------
    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if not message or message.author.bot:
            return
        if not self._match_natural_command(message.content or ""):
            return
        try:
            await self._send_embeds(message.channel, author=message.author)
        except Exception as e:
            log.exception("[channel-dir] failed to send directory: %s", e)

    @commands.command(name="channel", help="Show channel list")
    async def cmd_channel(self, ctx: commands.Context, *, sub: str = ""):
        # Support legacy "!channel list" if someone still uses it
        if "list" in (sub or "").lower():
            await self._send_embeds(ctx.channel, author=getattr(ctx, "author", None))

async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelDirectory(bot))
