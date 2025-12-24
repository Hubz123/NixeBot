# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord.ext import commands

USER_AGENT = "Mozilla/5.0 (compatible; NixeBot/1.0; +https://github.com/Hubz123/NixeBot)"

# --- Extract ytInitialPlayerResponse from /live HTML ---
_YTIPR_RE = re.compile(r"ytInitialPlayerResponse\s*=\s*(\{.*?\});", re.DOTALL)


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key, default) or default).strip()


def _as_int(val: str, default: int) -> int:
    try:
        return int(str(val).strip())
    except Exception:
        return default


def _resolve_path(p: str) -> Optional[pathlib.Path]:
    """
    Resolve a relative path in common Nixe layouts.
    We intentionally support:
      - ./data/...
      - ./DATA/...
      - ./NIXE/DATA/...
      - ./nixe/DATA/...
    """
    p = (p or "").strip()
    if not p:
        return None

    cand: List[pathlib.Path] = []
    raw = pathlib.Path(p)
    if raw.is_absolute():
        cand.append(raw)

    base = pathlib.Path(".").resolve()
    cand.extend([
        base / p,
        base / "data" / pathlib.Path(p).name,
        base / "DATA" / pathlib.Path(p).name,
        base / "NIXE" / "DATA" / pathlib.Path(p).name,
        base / "nixe" / "DATA" / pathlib.Path(p).name,
    ])

    seen = set()
    for c in cand:
        try:
            c = c.resolve()
        except Exception:
            continue
        if str(c) in seen:
            continue
        seen.add(str(c))
        if c.exists() and c.is_file():
            return c
    return None


def _extract_json_blob(html: str, rx: re.Pattern) -> Optional[Dict[str, Any]]:
    try:
        m = rx.search(html or "")
        if not m:
            return None
        blob = m.group(1)
        return json.loads(blob)
    except Exception:
        return None


def _yt_live_info(player: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], bool]:
    """
    Returns (video_id, title, is_live_now).
    Fail-closed for scheduled/upcoming streams: require isLiveNow == True.
    """
    vid = None
    title = None
    is_live_now = False

    vd = (player.get("videoDetails") or {}) if isinstance(player, dict) else {}
    vid = vd.get("videoId")
    title = vd.get("title")

    micro = (player.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    live = micro.get("liveBroadcastDetails") or {}
    if isinstance(live, dict) and ("isLiveNow" in live):
        is_live_now = bool(live.get("isLiveNow"))
    else:
        is_live_now = False

    return vid, title, is_live_now


@dataclass
class Target:
    name: str
    handle: str = ""
    channel_id: str = ""
    url: str = ""

    def base_url(self) -> str:
        if self.url:
            return self.url.strip()
        if self.handle:
            h = self.handle.strip()
            if not h:
                return ""
            if h.startswith("@"):
                return f"https://www.youtube.com/{h}"
            if h.startswith("http"):
                return h
            return f"https://www.youtube.com/@{h}"
        if self.channel_id:
            return f"https://www.youtube.com/channel/{self.channel_id}"
        return ""


class YouTubeWuWaTestLive(commands.Cog):
    """
    Text command handler similar to translate module (no slash).
    Commands:
      - nixe ytwtest
      - nixe ytwtest announce
      - nixe ytwtest here 3
      - nixe yt test   (alias)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cooldown: Dict[int, float] = {}  # user_id -> monotonic ts
        self.session: Optional[aiohttp.ClientSession] = None

        self.watchlist_path = _env("NIXE_YT_WUWA_WATCHLIST_PATH", "data/youtube_wuwa_watchlist.json")
        self.announce_channel_id = _as_int(_env("NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID", "0"), 0)

        # Match announcer template keys (same as a21)
        tmpl = _env("NIXE_YT_WUWA_TEMPLATE", "")
        if not tmpl:
            tmpl = "Hey, {creator.name} just posted a new live!\n{video.link}"
        self.template = tmpl

        # Optional title filter (same as a21)
        title_rx = _env("NIXE_YT_WUWA_TITLE_REGEX", "").strip()
        try:
            self.title_rx = re.compile(title_rx, re.IGNORECASE) if title_rx else None
        except Exception:
            self.title_rx = None

        # Safety knobs
        self.cooldown_sec = float(_as_int(_env("NIXE_YT_WUWA_TEST_COOLDOWN_SEC", "5"), 5))
        self.http_timeout = float(_as_int(_env("NIXE_YT_WUWA_TEST_HTTP_TIMEOUT", "25"), 25))

    async def cog_load(self):
        # Create session
        try:
            timeout = aiohttp.ClientTimeout(total=self.http_timeout)
            self.session = aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT})
        except Exception:
            self.session = None

    async def cog_unload(self):
        try:
            if self.session:
                await self.session.close()
        except Exception:
            pass
        self.session = None

    def _render_template(self, creator_name: str, video_link: str) -> str:
        msg = self.template
        msg = msg.replace("{creator.name}", creator_name)
        msg = msg.replace("{video.link}", video_link)
        return msg

    def _cooldown_ok(self, user_id: int) -> bool:
        now = asyncio.get_running_loop().time()
        last = float(self._cooldown.get(user_id) or 0.0)
        if (now - last) < self.cooldown_sec:
            return False
        self._cooldown[user_id] = now
        return True

    def _load_watchlist(self) -> List[Target]:
        p = _resolve_path(self.watchlist_path)
        if not p:
            return []
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
        out: List[Target] = []
        for it in (raw.get("targets") or []):
            if not isinstance(it, dict):
                continue
            out.append(
                Target(
                    name=str(it.get("name") or it.get("query") or it.get("handle") or it.get("channel_id") or "Unknown"),
                    handle=str(it.get("handle") or "").strip(),
                    channel_id=str(it.get("channel_id") or "").strip(),
                    url=str(it.get("url") or "").strip(),
                )
            )
        return out

    async def _http_get_text(self, url: str) -> Optional[str]:
        if not self.session:
            return None
        try:
            async with self.session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                return await resp.text()
        except Exception:
            return None

    async def _check_live(self, t: Target) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Returns (is_live_now, video_id, title).
        """
        base = t.base_url()
        if not base:
            return False, None, None
        live_url = base.rstrip("/") + "/live"
        html = await self._http_get_text(live_url)
        if not html:
            return False, None, None
        player = _extract_json_blob(html, _YTIPR_RE)
        if not player:
            return False, None, None
        vid, title, is_live_now = _yt_live_info(player)
        if self.title_rx and title and (not self.title_rx.search(title)):
            # Title doesn't match WuWa filter; treat as not-live for the purpose of announcements/tests
            return False, vid, title
        return bool(is_live_now), vid, title

    async def _post_preview(
        self,
        channel: discord.abc.Messageable,
        creator_name: str,
        base_url: str,
        is_live_now: bool,
        video_id: Optional[str],
        title: Optional[str],
        reference: Optional[discord.Message] = None,
    ):
        if is_live_now and video_id:
            video_link = f"https://youtu.be/{video_id}"
            content = self._render_template(creator_name, video_link)
            embed = discord.Embed(title=title or "LIVE", url=video_link)
            embed.set_author(name=creator_name)
            embed.set_image(url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Watch", url=video_link))
            await channel.send(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
                reference=reference,
            )
            return

        # Not live → still post a preview so you can see embed + button format
        content = f"[ytwtest] **NOT LIVE**: {creator_name}\n{base_url}"
        embed = discord.Embed(title=f"NOT LIVE — {creator_name}", url=base_url, description=(title or "").strip() or None)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Open Channel", url=base_url))
        await channel.send(
            content=content,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
            reference=reference,
        )

    async def on_message(self, message: discord.Message):
        if getattr(message.author, "bot", False):
            return
        if not message.guild:
            return

        content = (message.content or "").strip()
        if not content:
            return

        low = content.lower().strip()

        # Accept both:
        #   "nixe ytwtest ..."
        #   "nixe yt test ..."
        is_trigger = low.startswith("nixe ytwtest") or low.startswith("nixe yt test")
        if not is_trigger:
            return

        if not self._cooldown_ok(int(message.author.id)):
            return

        # Parse args
        tokens = content.split()
        # Normalize: if "nixe yt test" then pretend cmd="ytwtest"
        args = []
        if len(tokens) >= 3 and tokens[0].lower() == "nixe" and tokens[1].lower() == "yt" and tokens[2].lower() == "test":
            args = tokens[3:]
        else:
            # nixe ytwtest ...
            args = tokens[2:] if len(tokens) >= 2 else []

        where = "here"
        count = 1
        for a in args:
            al = a.lower().strip()
            if al in ("here", "announce"):
                where = al
                continue
            if al.isdigit():
                count = max(1, min(int(al), 3))

        targets = self._load_watchlist()
        if not targets:
            await message.channel.send("[ytwtest] Watchlist kosong / tidak ketemu.", reference=message)
            return

        # choose distinct targets
        picks = random.sample(targets, k=min(count, len(targets)))

        dest: discord.abc.Messageable = message.channel
        if where == "announce" and self.announce_channel_id:
            ch = self.bot.get_channel(self.announce_channel_id)
            if isinstance(ch, discord.TextChannel):
                dest = ch

        for t in picks:
            base = t.base_url()
            if not base:
                continue
            is_live_now, vid, title = await self._check_live(t)
            await self._post_preview(
                dest,
                creator_name=t.name,
                base_url=base,
                is_live_now=is_live_now,
                video_id=vid,
                title=title,
                reference=message if dest == message.channel else None,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeWuWaTestLive(bot))
