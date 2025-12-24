# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger("nixe.cogs.a21b_youtube_wuwa_test_command")

# Reuse the same defaults as the live announcer for consistent preview output.
WATCHLIST_PATH = os.getenv("NIXE_YT_WUWA_WATCHLIST_PATH", "data/youtube_wuwa_watchlist.json").strip() or "data/youtube_wuwa_watchlist.json"
TITLE_REGEX = (os.getenv("NIXE_YT_WUWA_TITLE_REGEX", "") or "").strip()
DEFAULT_TITLE_REGEX = r"#?鳴潮|Wuthering\s*Waves|WuWa"

ENV_TEMPLATE_OVERRIDE = os.getenv("NIXE_YT_WUWA_MESSAGE_TEMPLATE", "").strip()
DEFAULT_MESSAGE_TEMPLATE = "Hey, {creator.name} just posted a new video!\n{video.link}"

USER_AGENT = "Mozilla/5.0 (compatible; NixeBot/1.0; +https://github.com/Hubz123/NixeBot)"

# Optional safety toggle. Defaults ON to match user expectation.
TEST_CMD_ENABLE = os.getenv("NIXE_YT_WUWA_TEST_CMD_ENABLE", "1").strip() == "1"

# ----------------------------
# Helpers: safe JSON IO with multiple fallback paths
# ----------------------------
def _candidate_paths(p: str) -> List[str]:
    base = pathlib.Path(__file__).resolve().parents[2]  # repo root-ish (nixe/..)
    cands = [p]
    cands += [
        str(base / p),
        str(base / "data" / pathlib.Path(p).name),
        str(base / "DATA" / pathlib.Path(p).name),
        str(base / "nixe" / "data" / pathlib.Path(p).name),
        str(base / "nixe" / "DATA" / pathlib.Path(p).name),
    ]
    out: List[str] = []
    for x in cands:
        if x and x not in out:
            out.append(x)
    return out

def _read_json_any(p: str) -> Optional[Dict[str, Any]]:
    for cand in _candidate_paths(p):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            continue
        except Exception as e:
            log.warning("[yt-wuwa-test] failed to read json %s: %r", cand, e)
            continue
    return None

# ----------------------------
# YouTube parsing (HTML scrape, same principle as a21)
# ----------------------------
_YTIPR_RE = re.compile(r"ytInitialPlayerResponse\s*=\s*(\{.*?\});", re.DOTALL)

def _extract_json_blob(html: str, rx: re.Pattern) -> Optional[Dict[str, Any]]:
    m = rx.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None

def _yt_player_info(player: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[bool]]:
    """
    Returns (video_id, title, is_live_now or None if unknown).
    We DO NOT enforce live-only here because this is a preview/test command.
    """
    if not isinstance(player, dict):
        return None, None, None

    vd = player.get("videoDetails") or {}
    vid = vd.get("videoId")
    title = vd.get("title")

    micro = (player.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    live = micro.get("liveBroadcastDetails") or {}
    is_live_now: Optional[bool] = None
    if isinstance(live, dict) and ("isLiveNow" in live):
        is_live_now = bool(live.get("isLiveNow"))

    return vid, title, is_live_now

# ----------------------------
# Search resolve (name -> channelId) fallback
# ----------------------------
_YTINITDATA_RE = re.compile(r"ytInitialData\s*=\s*(\{.*?\});", re.DOTALL)

def _score_channel_hit(query: str, title: str) -> int:
    q = (query or "").lower()
    t = (title or "").lower()
    score = 0
    for tok in re.split(r"\s+", q):
        tok = tok.strip()
        if tok and tok in t:
            score += 2
    if q and q in t:
        score += 5
    return score

def _pick_best_channel(query: str, candidates: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    best = None
    best_score = -1
    for cid, title in candidates:
        s = _score_channel_hit(query, title)
        if s > best_score:
            best_score = s
            best = (cid, title)
    return best

def _collect_channel_renderers(node: Any, out: List[Tuple[str, str]]) -> None:
    if isinstance(node, dict):
        if "channelRenderer" in node and isinstance(node["channelRenderer"], dict):
            cr = node["channelRenderer"]
            cid = cr.get("channelId")
            t = (((cr.get("title") or {}).get("simpleText")) or "")
            if cid and t:
                out.append((cid, t))
        for v in node.values():
            _collect_channel_renderers(v, out)
    elif isinstance(node, list):
        for it in node:
            _collect_channel_renderers(it, out)

@dataclass
class Target:
    name: str
    query: str
    handle: str = ""
    channel_id: str = ""
    url: str = ""

    def base_url(self) -> Optional[str]:
        if self.url:
            return self.url.rstrip("/")
        if self.handle:
            h = self.handle.strip()
            if not h:
                return None
            if h.startswith("@"):
                return f"https://www.youtube.com/{h}"
            return f"https://www.youtube.com/@{h}"
        if self.channel_id:
            return f"https://www.youtube.com/channel/{self.channel_id}"
        return None

class YouTubeWuWaTestCommand(commands.Cog):
    """
    Prefix command:
      nixe ytwtest [here|announce] [count=N]

    - Picks N random channels from WuWa watchlist and posts a preview embed (identical style to a21).
    - This command does NOT write to state and does NOT affect dedup.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.title_rx = re.compile((TITLE_REGEX or DEFAULT_TITLE_REGEX), re.UNICODE)
        self.template = (ENV_TEMPLATE_OVERRIDE or DEFAULT_MESSAGE_TEMPLATE).strip() or DEFAULT_MESSAGE_TEMPLATE

        self.watch: Dict[str, Any] = _read_json_any(WATCHLIST_PATH) or {}
        self.targets: List[Target] = self._load_targets(self.watch)

    def _load_targets(self, cfg: Dict[str, Any]) -> List[Target]:
        tlist = cfg.get("targets") or []
        out: List[Target] = []
        for t in tlist:
            if isinstance(t, str):
                name = t.strip()
                if name:
                    out.append(Target(name=name, query=name))
                continue
            if isinstance(t, dict):
                name = str(t.get("name") or "").strip()
                if not name:
                    continue
                out.append(Target(
                    name=name,
                    query=str(t.get("query") or name),
                    handle=str(t.get("handle") or ""),
                    channel_id=str(t.get("channel_id") or ""),
                    url=str(t.get("url") or ""),
                ))
        return out

    async def _ensure_session(self):
        if self.session and not self.session.closed:
            return
        timeout = aiohttp.ClientTimeout(total=25)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "ja,en-US;q=0.9,id;q=0.8",
            },
        )

    async def cog_unload(self):
        try:
            if self.session and not self.session.closed:
                await self.session.close()
        except Exception:
            pass

    async def _http_get_text(self, url: str) -> Optional[str]:
        await self._ensure_session()
        assert self.session is not None
        try:
            async with self.session.get(url, allow_redirects=True) as r:
                if r.status != 200:
                    return None
                return await r.text()
        except Exception:
            return None

    async def _resolve_channel_id(self, t: Target) -> Target:
        # If already resolvable, keep.
        if t.channel_id or t.url or t.handle:
            return t
        # Search YouTube channels for query
        q = quote_plus(t.query or t.name)
        search_url = f"https://www.youtube.com/results?search_query={q}&sp=EgIQAg%253D%253D"
        html = await self._http_get_text(search_url)
        if not html:
            return t
        init = _extract_json_blob(html, _YTINITDATA_RE)
        if not init:
            return t

        cand: List[Tuple[str, str]] = []
        _collect_channel_renderers(init, cand)
        best = _pick_best_channel(t.query or t.name, cand)
        if not best:
            return t

        cid, title = best
        t.channel_id = cid
        t.url = f"https://www.youtube.com/channel/{cid}"
        t.name = title or t.name
        return t

    def _render_template(self, creator_name: str, video_link: str) -> str:
        msg = self.template
        msg = msg.replace("{creator.name}", creator_name)
        msg = msg.replace("{video.link}", video_link)
        return msg

    async def _build_preview(self, t: Target) -> Tuple[str, discord.Embed, discord.ui.View]:
        """
        Returns (content, embed, view) with same layout as a21.
        """
        t = await self._resolve_channel_id(t)
        base = t.base_url() or "https://www.youtube.com"

        # Attempt to get /live player info (works even if not live; we will label the status).
        html = await self._http_get_text(base.rstrip("/") + "/live")
        vid = None
        title = None
        is_live_now: Optional[bool] = None
        if html:
            player = _extract_json_blob(html, _YTIPR_RE)
            if player:
                vid, title, is_live_now = _yt_player_info(player)

        # Choose link for preview:
        # - If we found a video id, link to it (shows normal YouTube preview).
        # - Otherwise, link to the channel.
        if vid:
            video_link = f"https://youtu.be/{vid}"
        else:
            video_link = base

        # Title: prefer player title; else fallback to a deterministic test title.
        if title:
            title_out = title
        else:
            title_out = f"[TEST] Live Announce Preview ({t.name})"

        # Mark status visibly (no new config; just test output).
        status_str = "UNKNOWN"
        if is_live_now is True:
            status_str = "LIVE"
        elif is_live_now is False:
            status_str = "NOT LIVE"

        content = self._render_template(t.name, video_link)
        content = f"[TEST] {content}"

        embed = discord.Embed(title=title_out, url=video_link, description=f"Status: **{status_str}**")
        embed.set_author(name=t.name)

        if vid:
            embed.set_image(url=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
        else:
            # No vid -> no thumbnail; keep embed minimal.
            pass

        # Small diagnostics to help you confirm the resolved channel base
        try:
            embed.add_field(name="Channel", value=base, inline=False)
        except Exception:
            pass

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Watch", url=video_link))
        return content, embed, view

    @commands.command(name="ytwtest", aliases=["ytwuwa_test", "yt_wuwa_test"])
    @commands.guild_only()
    async def ytwtest(self, ctx: commands.Context, where: str = "here", count: str = "1"):
        """
        Usage:
          nixe ytwtest
          nixe ytwtest here 1
          nixe ytwtest announce 1
          nixe ytwtest here 3

        - where: 'here' or 'announce'
        - count: how many random channels to preview (1..3)
        """
        if not TEST_CMD_ENABLE:
            return

        # Permission guard: keep lightweight but prevent random spam.
        try:
            perms = ctx.channel.permissions_for(ctx.author)  # type: ignore
            if not (perms.manage_guild or perms.administrator):
                await ctx.reply("❌ Butuh permission **Manage Server** untuk pakai command test ini.", mention_author=False)
                return
        except Exception:
            pass

        # Reload watchlist every call (so edits are immediate)
        self.watch = _read_json_any(WATCHLIST_PATH) or self.watch
        self.targets = self._load_targets(self.watch) or self.targets

        if not self.targets:
            await ctx.reply("❌ Watchlist kosong / tidak terbaca.", mention_author=False)
            return

        try:
            n = int(str(count).strip())
        except Exception:
            n = 1
        n = max(1, min(3, n))

        where_norm = (where or "here").strip().lower()
        dest: Optional[discord.TextChannel] = None
        if where_norm.startswith("ann"):
            # Reuse same announce channel id env as a21
            try:
                cid = int(os.getenv("NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID", "0") or "0")
            except Exception:
                cid = 0
            ch = self.bot.get_channel(cid) if cid else None
            if isinstance(ch, discord.TextChannel):
                dest = ch
            else:
                dest = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
        else:
            dest = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None

        if not isinstance(dest, discord.TextChannel):
            await ctx.reply("❌ Channel tujuan tidak valid.", mention_author=False)
            return

        picks = random.sample(self.targets, k=min(n, len(self.targets)))
        # Acknowledge quickly (so Discord doesn't feel laggy)
        try:
            await ctx.reply(f"🧪 Testing YouTube embed preview: **{len(picks)}** channel (random).", mention_author=False)
        except Exception:
            pass

        for t in picks:
            content, embed, view = await self._build_preview(t)
            await dest.send(content=content, embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
            await asyncio.sleep(0.8)

async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeWuWaTestCommand(bot))
