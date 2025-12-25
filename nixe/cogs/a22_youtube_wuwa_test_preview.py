# -*- coding: utf-8 -*-
from __future__ import annotations

"""
a22_youtube_wuwa_test_preview.py

Text-trigger test command for YouTube LIVE status, aligned with the "nixe ..." translate-style pattern.

Triggers (message content):
- "nixe ytwtest" [here|announce] [count]
- "nixe yt test" [here|announce] [count]

Behavior:
- Picks random target(s) from watchlist (default 35)
- For each target, checks /live page and parses ytInitialPlayerResponse
- Posts an embed + link button:
  - LIVE -> Watch button to youtu.be/<videoId>
  - NOT LIVE -> Open Channel button to channel URL

No slash commands. No moderator-gating.
"""

import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger(__name__)


@dataclass
class Target:
    name: str
    handle: str
    channel_id: str
    url: str

    def channel_url(self) -> str:
        if self.url:
            return self.url
        if self.channel_id:
            return f"https://www.youtube.com/channel/{self.channel_id}"
        if self.handle:
            h = self.handle if self.handle.startswith("@") else f"@{self.handle}"
            return f"https://www.youtube.com/{h}"
        return "https://www.youtube.com/"

    def live_url(self) -> str:
        if self.channel_id:
            return f"https://www.youtube.com/channel/{self.channel_id}/live"
        if self.handle:
            h = self.handle if self.handle.startswith("@") else f"@{self.handle}"
            return f"https://www.youtube.com/{h}/live"
        # fallback
        cu = self.channel_url().rstrip("/")
        return f"{cu}/live"


def _try_paths(path_str: str) -> list[Path]:
    """
    Produce a small set of candidate paths for watchlist/state that matches the repo conventions:
    - exact path
    - ./path
    - ./data/<basename>
    - ./DATA/<basename>
    """
    p = Path(path_str)
    b = p.name if p.name else "youtube_wuwa_watchlist.json"
    cands = [
        p,
        Path(".") / p,
        Path("data") / b,
        Path("DATA") / b,
        Path("NIXE") / "DATA" / b,
        Path("nixe") / "DATA" / b,
    ]
    # de-dup while preserving order
    out: list[Path] = []
    seen = set()
    for c in cands:
        cc = c.resolve() if c.exists() else c
        key = str(cc)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def load_watchlist() -> Tuple[bool, str, list[Target]]:
    watchlist_path = os.environ.get("NIXE_YT_WUWA_WATCHLIST_PATH", "data/youtube_wuwa_watchlist.json").strip()
    for cand in _try_paths(watchlist_path):
        try:
            if cand.exists():
                data = json.loads(cand.read_text(encoding="utf-8"))
                targets_raw = data.get("targets", [])
                targets: list[Target] = []
                for t in targets_raw:
                    targets.append(
                        Target(
                            name=str(t.get("name", "")).strip(),
                            handle=str(t.get("handle", "")).strip(),
                            channel_id=str(t.get("channel_id", "")).strip(),
                            url=str(t.get("url", "")).strip(),
                        )
                    )
                targets = [t for t in targets if (t.handle or t.channel_id or t.url)]
                if not targets:
                    return False, f"watchlist empty in {cand}", []
                return True, f"{cand}", targets
        except Exception as e:
            log.warning("[ytwtest] failed read json %s: %r", cand, e)
            continue
    return False, f"watchlist not found. env NIXE_YT_WUWA_WATCHLIST_PATH={watchlist_path}", []


def _extract_json_object(text: str, key: str = "ytInitialPlayerResponse") -> Optional[dict]:
    """
    Extract JSON object assigned to ytInitialPlayerResponse using brace matching.
    Returns dict or None.
    """
    # find assignment location
    m = re.search(rf"{re.escape(key)}\s*=\s*", text)
    if not m:
        # other common pattern: "var ytInitialPlayerResponse = ..."
        m = re.search(rf"var\s+{re.escape(key)}\s*=\s*", text)
    if not m:
        return None

    i = m.end()
    # skip whitespace
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text) or text[i] != "{":
        return None

    depth = 0
    start = i
    for j in range(i, len(text)):
        ch = text[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = text[start : j + 1]
                try:
                    return json.loads(blob)
                except Exception:
                    return None
    return None


def _live_from_player(player: dict) -> Tuple[bool, str, str]:
    """
    Returns (is_live_now, video_id, title)
    """
    video = player.get("videoDetails") or {}
    vid = str(video.get("videoId") or "").strip()
    title = str(video.get("title") or "").strip()

    micro = (player.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    if not title:
        t = micro.get("title") or {}
        if isinstance(t, dict):
            title = str(t.get("simpleText") or "").strip()

    live_details = micro.get("liveBroadcastDetails") or {}
    is_live_now = bool(live_details.get("isLiveNow") is True)

    return is_live_now, vid, title


class YouTubeWuWaTestPreview(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cooldown: dict[int, float] = {}
        self._session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        # reuse session if app has one; otherwise create
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NixeBot/ytwtest",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _ch_label(self, ch: object) -> str:
        """Best-effort channel label for logs."""
        try:
            cid = getattr(ch, 'id', 'unknown')
            name = getattr(ch, 'name', None)
            if name:
                return f"{name}({cid})"
            return str(cid)
        except Exception:
            return 'unknown'

    def _rate_limited(self, uid: int, cooldown_s: float = 5.0) -> bool:
        now = asyncio.get_event_loop().time()
        last = self._cooldown.get(uid, 0.0)
        if now - last < cooldown_s:
            return True
        self._cooldown[uid] = now
        return False

    async def _fetch_live(self, target: Target) -> Tuple[bool, str, str]:
        """
        Returns (is_live_now, video_id, title). Fail-closed: if parsing fails, returns (False,"","").
        """
        if not self._session:
            return False, "", ""

        url = target.live_url()
        try:
            async with self._session.get(url, allow_redirects=True) as resp:
                html = await resp.text(errors="ignore")
        except Exception as e:
            log.warning("[ytwtest] fetch failed %s: %r", url, e)
            return False, "", ""

        player = _extract_json_object(html, "ytInitialPlayerResponse")
        if not player:
            return False, "", ""

        is_live_now, vid, title = _live_from_player(player)
        return is_live_now, vid, title

    async def _safe_send(self, channel: discord.abc.Messageable, *, embed: discord.Embed, view: Optional[discord.ui.View] = None):
        try:
            await channel.send(embed=embed, view=view)
            return
        except discord.Forbidden:
            log.warning("[ytwtest] Forbidden: cannot send to channel=%s", self._ch_label(channel))
        except Exception as e:
            log.warning("[ytwtest] send failed: %r", e)

    def _build_view(self, label: str, url: str) -> discord.ui.View:
        v = discord.ui.View()
        v.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
        return v

    def _template_content(self, target: Target, video_url: str) -> str:
        tpl = os.environ.get("NIXE_YT_WUWA_MESSAGE_TEMPLATE", "").strip()
        if not tpl:
            return ""  # keep content empty; embed carries the info
        # Minimal replacements compatible with earlier templates
        out = tpl.replace("{creator.name}", target.name or "Creator")
        out = out.replace("{video.link}", video_url)
        return out

    async def _run(self, where: str, count: int, invoke_channel: discord.abc.Messageable, guild: Optional[discord.Guild]) -> None:
        ok, wl_path, targets = load_watchlist()
        if not ok:
            emb = discord.Embed(title="ytwtest error", description=wl_path)
            await self._safe_send(invoke_channel, embed=emb)
            return

        # destination resolve
        dest: discord.abc.Messageable = invoke_channel
        if where == "announce" and guild is not None:
            ann = os.environ.get("NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID", "").strip()
            if not ann.isdigit():
                await self._safe_send(
                    invoke_channel,
                    embed=discord.Embed(
                        title="ytwtest announce not configured",
                        description="Set env NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID to a channel ID, or use 'here'.",
                    ),
                )
            else:
                ch = guild.get_channel(int(ann))
                if ch is None:
                    try:
                        ch = await guild.fetch_channel(int(ann))
                    except Exception:
                        ch = None
                if ch is None:
                    await self._safe_send(
                        invoke_channel,
                        embed=discord.Embed(
                            title="ytwtest announce channel not found",
                            description=f"NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID={ann} could not be resolved. Posting to the invoke channel instead.",
                        ),
                    )
                else:
                    dest = ch


        picks = random.sample(targets, k=min(count, len(targets)))
        for t in picks:
            is_live, vid, title = await self._fetch_live(t)
            if is_live and vid:
                video_url = f"https://youtu.be/{vid}"
                emb = discord.Embed(title=f"🔴 LIVE NOW: {t.name}", description=title or "Live")
                emb.add_field(name="Channel", value=t.channel_url(), inline=False)
                emb.add_field(name="Watch", value=video_url, inline=False)
                view = self._build_view("Watch", video_url)
            else:
                video_url = t.channel_url()
                emb = discord.Embed(title=f"⚪ NOT LIVE (test): {t.name}", description="No live stream detected right now.")
                emb.add_field(name="Channel", value=t.channel_url(), inline=False)
                view = self._build_view("Open Channel", t.channel_url())

            content = self._template_content(t, video_url)
            try:
                await dest.send(content=content or None, embed=emb, view=view)
            except discord.Forbidden:
                log.warning("[ytwtest] Forbidden: cannot send to channel=%s", self._ch_label(dest))
                # try fallback to invoke_channel
                await self._safe_send(invoke_channel, embed=emb, view=view)
            except Exception as e:
                log.warning("[ytwtest] post failed: %r", e)
                await self._safe_send(invoke_channel, embed=discord.Embed(title="ytwtest send error", description=str(e)))

            await asyncio.sleep(0.6)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if getattr(message.author, "bot", False):
            return
        if message.guild is None:
            return

        content = (message.content or "").strip()
        if not content:
            return
        low = content.lower().strip()

        # Accept: "nixe ytwtest ..." and "nixe yt test ..."
        if not (low.startswith("nixe ytwtest") or low.startswith("nixe yt test")):
            return

        uid = getattr(message.author, "id", 0) or 0
        if uid and self._rate_limited(uid, cooldown_s=5.0):
            return

        # tokens minimal: ["nixe","ytwtest"] or ["nixe","yt","test"]
        parts = content.split()
        where = "here"
        count = 1

        if low.startswith("nixe ytwtest"):
            # nixe ytwtest [announce|here] [count]
            if len(parts) >= 3 and parts[2].lower() in ("announce", "here"):
                where = parts[2].lower()
            if len(parts) >= 4:
                try:
                    count = int(parts[3])
                except Exception:
                    count = 1
        else:
            # nixe yt test [announce|here] [count]
            if len(parts) >= 4 and parts[3].lower() in ("announce", "here"):
                where = parts[3].lower()
            if len(parts) >= 5:
                try:
                    count = int(parts[4])
                except Exception:
                    count = 1

        count = max(1, min(int(count), 3))
        log.info("[ytwtest] invoke uid=%s ch=%s where=%s count=%s guild=%s", uid, self._ch_label(message.channel), where, count, getattr(message.guild, "id", "0"))

        try:
            await self._run(where, count, message.channel, message.guild)
        except Exception as e:
            log.exception("[ytwtest] run failed: %r", e)
            try:
                await message.reply(f"❌ ytwtest internal error: {e}", mention_author=False)
            except Exception:
                pass

    # Optional prefix fallback (does not affect the on_message trigger path)
    @commands.command(name="ytwpreview", aliases=["ytwtest_preview"])
    async def cmd_ytwpreview(self, ctx: commands.Context, where: str = "here", count: int = 1):
        where = (where or "here").lower()
        if where not in ("here", "announce"):
            where = "here"
        count = max(1, min(int(count), 3))
        await ctx.reply("✅ ytwtest running…", mention_author=False)
        await self._run(where, count, ctx.channel, ctx.guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeWuWaTestPreview(bot))
