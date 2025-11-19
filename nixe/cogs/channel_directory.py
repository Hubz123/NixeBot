# -*- coding: utf-8 -*-
"""
Channel directory helper.

Plain-text trigger (no prefix):
    - "nixe channel list"
    - etc. (configurable via CHANLIST_TRIGGERS)

Reads a declarative JSON config (channel_directory.json) and renders
a single, tidy embed which is automatically deleted together with the
trigger message after a short TTL.

This completely replaces the old LPG / PHISHING / MISC templates so
there is no double template any more.
"""

import os
import re
import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
import json

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


def _resolve_config_path() -> Path:
    """
    Determine the path to channel_directory.json.

    Priority:
    1) Explicit CHANLIST_CONFIG (absolute or relative)
    2) ../config/channel_directory.json (nixe/config)
    3) ./channel_directory.json (same folder as this file)
    """
    override = _env("CHANLIST_CONFIG", "").strip()
    if override:
        p = Path(override)
        if not p.is_absolute():
            # resolve relative to this file
            base = Path(__file__).resolve().parent
            p = (base / p).resolve()
        if p.is_file():
            return p

    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "config" / "channel_directory.json",
        here.parent / "channel_directory.json",
    ]
    for c in candidates:
        if c.is_file():
            return c

    # last resort: assume ../config/channel_directory.json even if missing,
    # so the log message at least tells user where we looked.
    return candidates[0]


def _load_config() -> Dict[str, Any]:
    path = _resolve_config_path()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        log.info("[chan-list] using config %s", path)
        return data or {}
    except FileNotFoundError:
        log.warning("[chan-list] config not found at %s; using fallback", path)
    except Exception as e:
        log.exception("[chan-list] failed to load config %s: %r", path, e)

    # minimal safe default if JSON missing / broken
    return {
        "title": "📌 Direktori Channel & Thread",
        "color": "#60a5fa",
        "footer": "Config channel_directory.json tidak ditemukan atau invalid.",
        "compact": 1,
        "sections": [],
    }


def _mention(ch_id: Optional[int]) -> str:
    try:
        if not ch_id:
            return "-"
        if int(ch_id) <= 0:
            return "-"
        return f"<#{int(ch_id)}>"
    except Exception:
        return "-"


class ChannelDirectory(commands.Cog):
    """Single-embed channel directory driven by channel_directory.json."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.triggers = self._load_triggers()
        self.ttl = max(1, int(_env("CHANLIST_TTL_SEC", "10")))
        self.cooldown = max(0, int(_env("CHANLIST_COOLDOWN_SEC", "3")))
        self._recent: set[int] = set()
        self.config: Dict[str, Any] = _load_config()
        log.info(
            "[chan-list] ready; ttl=%ss cooldown=%ss triggers=%s",
            self.ttl,
            self.cooldown,
            self.triggers,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #

    def _load_triggers(self) -> List[str]:
        raw = _env("CHANLIST_TRIGGERS", "")
        if raw.strip():
            arr = [
                x.strip().lower()
                for x in re.split(r"[,\\n;]", raw)
                if x.strip()
            ]
            if arr:
                return arr
        return [t.lower() for t in TRIGGERS_DEFAULT]

    def _build_embed(self) -> discord.Embed:
        cfg = self.config or {}
        title = cfg.get("title") or "📌 Direktori Channel & Thread"

        # color: hex string "#rrggbb"
        color_raw = str(cfg.get("color", "#60a5fa")).lstrip("#")
        try:
            color = discord.Color(int(color_raw, 16))
        except Exception:
            color = discord.Color(0x60A5FA)

        embed = discord.Embed(title=title, color=color)

        footer = cfg.get("footer")
        if isinstance(footer, str) and footer.strip():
            embed.set_footer(text=footer.strip())

        compact = bool(cfg.get("compact", 1))

        sections = cfg.get("sections") or []
        for section in sections:
            if not isinstance(section, dict):
                continue
            sec_title = section.get("title") or "​"  # zero-width space
            items = section.get("items") or []

            lines: List[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_name = str(item.get("name", "{channel}"))
                raw_id = item.get("id") or 0
                try:
                    ch_id = int(raw_id)
                except Exception:
                    ch_id = 0

                mention = _mention(ch_id)
                rendered = raw_name.replace("{channel}", mention)

                if compact:
                    lines.append(rendered)
                else:
                    lines.append(f"• {rendered}")

            value = "\n".join(lines) if lines else "-"
            embed.add_field(name=sec_title, value=value, inline=False)

        return embed

    async def _delete_later(self, *msgs: discord.Message):
        await asyncio.sleep(self.ttl)
        for m in msgs:
            try:
                await m.delete()
            except Exception:
                # jangan spam log kalau user sudah hapus manual
                pass

    def _match_trigger(self, content: str) -> bool:
        c = content.lower().strip()
        return any(c == t or c.startswith(t) for t in self.triggers)

    # ------------------------------------------------------------------ #
    # Events                                                             #
    # ------------------------------------------------------------------ #

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

        try:
            embed = self._build_embed()
            sent = await message.channel.send(embed=embed)
            # schedule deletion of bot message + trigger
            asyncio.create_task(self._delete_later(message, sent))
        except Exception as e:
            log.exception("channel list send failed: %r", e)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelDirectory(bot))
