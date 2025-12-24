# -*- coding: utf-8 -*-
from __future__ import annotations

"""
a22_youtube_wuwa_test_preview.py

Text-command YouTube LIVE test (no slash), designed to behave like c20_translate_commands text handler:
- Trigger: "nixe ytwtest ..." or "nixe yt test ..."
- Uses the SAME env keys as a21_youtube_wuwa_live_announce:
    NIXE_YT_WUWA_WATCHLIST_PATH
    NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID
    NIXE_YT_WUWA_MESSAGE_TEMPLATE
- Picks random channel(s) from watchlist and checks /live page.
- Posts a preview message with embed + link button. Always posts a result:
    - LIVE -> embed Watch link to video
    - NOT LIVE / scheduled / unknown -> embed Open Channel link

This cog does NOT write state and is safe for testing formatting.
"""

import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger(__name__)

# Env (reuse a21 keys)
WATCHLIST_PATH = (os.getenv("NIXE_YT_WUWA_WATCHLIST_PATH", "data/youtube_wuwa_watchlist.json") or "").strip() or "data/youtube_wuwa_watchlist.json"
ANNOUNCE_CHANNEL_ID = (os.getenv("NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID", "0") or "0").strip()
ENV_TEMPLATE_OVERRIDE = (os.getenv("NIXE_YT_WUWA_MESSAGE_TEMPLATE", "") or "").strip()
DEFAULT_MESSAGE_TEMPLATE = "Hey, {creator.name} just posted a new video!\n{video.link}"

# Safety toggles
TEST_ENABLE = (os.getenv("NIXE_YT_WUWA_TEST_CMD_ENABLE", "1") or "1").strip() == "1"
COOLDOWN_SEC = float((os.getenv("NIXE_YT_WUWA_TEST_COOLDOWN_SEC", "5") or "5").strip() or "5")

USER_AGENT = "Mozilla/5.0 (compatible; NixeBot/1.0; +https://github.com/Hubz123/NixeBot)"

# Regex to locate ytInitialPlayerResponse
_YTIPR_MARKERS = (
    "ytInitialPlayerResponse",
    "var ytInitialPlayerResponse",
    "window[\"ytInitialPlayerResponse\"]",
)

@dataclass
class Target:
    name: str = ""
    handle: str = ""
    channel_id: str = ""
    url: str = ""

    def base_url(self) -> str:
        u = (self.url or "").strip()
        if u.startswith("http"):
            return u.rstrip("/")
        h = (self.handle or "").strip()
        if h.startswith("@"):
            return f"https://www.youtube.com/{h}".rstrip("/")
        cid = (self.channel_id or "").strip()
        if cid:
            return f"https://www.youtube.com/channel/{cid}".rstrip("/")
        return ""

def _candidate_paths(rel: str) -> List[str]:
    rel = (rel or "").strip().lstrip("/\\")
    if not rel:
        return []
    return [
        rel,
        os.path.join("data", os.path.basename(rel)),
        os.path.join("DATA", os.path.basename(rel)),
        os.path.join("nixe", rel),
        os.path.join("nixe", "data", os.path.basename(rel)),
        os.path.join("NIXE", "DATA", os.path.basename(rel)),
    ]

def _read_json_any(path: str) -> Optional[Dict[str, Any]]:
    for cand in _candidate_paths(path):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            continue
        except Exception as e:
            log.warning("[ytwtest] failed read json %s: %r", cand, e)
            continue
    return None

def _render_template(creator_name: str, link: str) -> str:
    tmpl = (ENV_TEMPLATE_OVERRIDE or DEFAULT_MESSAGE_TEMPLATE or "").strip()
    if not tmpl:
        tmpl = DEFAULT_MESSAGE_TEMPLATE
    return tmpl.replace("{creator.name}", creator_name).replace("{video.link}", link)

def _load_targets_from_watchlist() -> List[Target]:
    cfg = _read_json_any(WATCHLIST_PATH) or {}
    targets = cfg.get("targets") or []
    out: List[Target] = []
    if isinstance(targets, list):
        for t in targets:
            if not isinstance(t, dict):
                continue
            out.append(Target(
                name=str(t.get("name") or ""),
                handle=str(t.get("handle") or ""),
                channel_id=str(t.get("channel_id") or ""),
                url=str(t.get("url") or ""),
            ))
    return out

def _extract_json_blob_from_marker(html: str, marker: str) -> Optional[dict]:
    """
    Very robust bracket-matching extraction for ytInitialPlayerResponse.
    """
    if not html or marker not in html:
        return None
    idx = html.find(marker)
    if idx < 0:
        return None
    # Find first '{' after marker
    brace = html.find("{", idx)
    if brace < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(brace, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == "\"":
                in_str = False
            continue
        else:
            if ch == "\"":
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blob = html[brace:i+1]
                    try:
                        return json.loads(blob)
                    except Exception:
                        return None
    return None

async def _http_get_text(session: aiohttp.ClientSession, url: str, timeout_s: int = 20) -> str:
    try:
        async with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
            if resp.status != 200:
                return ""
            return await resp.text()
    except Exception:
        return ""

def _parse_live_from_player(player: dict) -> Tuple[bool, str, str]:
    """
    Returns (is_live_now, video_id, title).
    Fail-closed: scheduled/upcoming returns is_live_now=False.
    """
    try:
        video_id = str(player.get("videoDetails", {}).get("videoId") or "")
        title = str(player.get("videoDetails", {}).get("title") or "YouTube Live")
        lbs = player.get("liveBroadcastDetails") or {}
        is_live_now = bool(lbs.get("isLiveNow") is True)
        return is_live_now, video_id, title
    except Exception:
        return False, "", "YouTube Live"

async def _check_target_live(session: aiohttp.ClientSession, t: Target) -> Tuple[Target, bool, str, str]:
    """
    Returns (target, is_live_now, video_id, title)
    """
    base = t.base_url()
    if not base:
        return t, False, "", ""
    live_url = base.rstrip("/") + "/live"
    html = await _http_get_text(session, live_url, timeout_s=20)
    if not html:
        return t, False, "", ""
    player = None
    # Try multiple markers
    for m in _YTIPR_MARKERS:
        player = _extract_json_blob_from_marker(html, m)
        if player:
            break
    if not player:
        # fallback regex attempt for "ytInitialPlayerResponse": {...}
        m = re.search(r"\"ytInitialPlayerResponse\"\s*:\s*(\{)", html)
        if m:
            player = _extract_json_blob_from_marker(html, "ytInitialPlayerResponse")
    if not player:
        return t, False, "", ""
    is_live, vid, title = _parse_live_from_player(player)
    return t, is_live, vid, title

class YouTubeWuWaTestPreview(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self._cooldowns: Dict[int, float] = {}

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    def _cooldown_ok(self, user_id: int) -> bool:
        import time
        now = time.time()
        last = float(self._cooldowns.get(user_id, 0.0))
        if now - last < COOLDOWN_SEC:
            return False
        self._cooldowns[user_id] = now
        return True

    async def _safe_send(self, channel: discord.abc.Messageable, *, content: str = "", embed: Optional[discord.Embed] = None, view: Optional[discord.ui.View] = None):
        try:
            await channel.send(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        except discord.Forbidden:
            # Can't send here; nothing else we can do besides logging.
            log.warning("[ytwtest] Forbidden: cannot send to channel=%s", getattr(channel, "id", "unknown"))
        except Exception as e:
            log.warning("[ytwtest] send failed: %r", e)

    async def _post_preview(self, dest: discord.abc.Messageable, t: Target, is_live: bool, vid: str, title: str):
        base = t.base_url() or (t.url or "").strip()
        creator = t.name or t.handle or t.channel_id or "YouTube"
        if is_live and vid:
            link = f"https://youtu.be/{vid}"
            content = _render_template(creator, link)
            embed = discord.Embed(title=title or "LIVE", url=link)
            embed.set_author(name=creator)
            embed.set_image(url=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Watch", url=link))
            await self._safe_send(dest, content=content, embed=embed, view=view)
        else:
            # Not live: still post a visible test result.
            ch_link = base if base.startswith("http") else ""
            embed = discord.Embed(title="NOT LIVE (test)", description=f"{creator}\n\nTidak sedang LIVE sekarang.")
            if ch_link:
                embed.url = ch_link
            view = discord.ui.View(timeout=None)
            if ch_link:
                view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Open Channel", url=ch_link))
            await self._safe_send(dest, content="", embed=embed, view=view)

    async def _run(self, where: str, count: int, src_channel: discord.abc.Messageable, author_id: int) -> str:
        if not TEST_ENABLE:
            return "❌ YT test command disabled (NIXE_YT_WUWA_TEST_CMD_ENABLE=0)."
        if not self._cooldown_ok(author_id):
            return "⏳ Cooldown. Coba lagi sebentar."
        targets = _load_targets_from_watchlist()
        if not targets:
            return f"❌ Watchlist tidak ditemukan / kosong. Path: {WATCHLIST_PATH}"

        # destination
        dest: discord.abc.Messageable = src_channel
        if where == "announce":
            if not ANNOUNCE_CHANNEL_ID or ANNOUNCE_CHANNEL_ID == "0":
                return "❌ Announce channel belum di-set. NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID=0"
            try:
                ch = self.bot.get_channel(int(ANNOUNCE_CHANNEL_ID))
            except Exception:
                ch = None
            if ch is None:
                return f"❌ Announce channel tidak ketemu. NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID={ANNOUNCE_CHANNEL_ID}"
            dest = ch  # type: ignore

        # pick random targets
        sample = random.sample(targets, k=min(count, len(targets)))

        await self._ensure_session()
        assert self._session is not None

        # run sequential (test-only, avoid bursts)
        for t in sample:
            try:
                tt, is_live, vid, title = await _check_target_live(self._session, t)
                await self._post_preview(dest, tt, is_live, vid, title)
            except Exception as e:
                log.warning("[ytwtest] check/post failed (%s): %r", t.name, e)
                await self._safe_send(dest, content=f"❌ ytwtest error for {t.name}: {e}")

        return f"OK. Posted {len(sample)} test result(s) to {where}."

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_session()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Mimic translate text-command behavior
        if getattr(message.author, "bot", False):
            return
        if not message.guild:
            return
        content = (message.content or "").strip()
        if not content:
            return
        low = content.lower()
        # Accept: "nixe ytwtest ..." and "nixe yt test ..."
        if not (low.startswith("nixe ytwtest") or low.startswith("nixe yt test")):
            return

        # Parse tokens like translate does
        parts = content.split()
        # tokens minimal: ["nixe","ytwtest"] or ["nixe","yt","test"]
        where = "here"
        count = 1

        # normalize forms
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

        log.info("[ytwtest] invoke uid=%s ch=%s where=%s count=%s", getattr(message.author, "id", "0"), getattr(message.channel, "id", "0"), where, count)

        # Always respond in some way (reply if possible, else channel send)
        try:
            result = await self._run(where, count, message.channel, int(message.author.id))
        except Exception as e:
            log.exception("[ytwtest] run failed: %r", e)
            result = f"❌ ytwtest internal error: {e}"

        # Prefer reply, fallback to send
        try:
            await message.reply(result, mention_author=False)
        except Exception:
            try:
                await message.channel.send(result, allowed_mentions=discord.AllowedMentions.none())
            except Exception as e:
                log.warning("[ytwtest] cannot send result message: %r", e)

    async def cog_unload(self):
        try:
            if self._session and not self._session.closed:
                await self._session.close()
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeWuWaTestPreview(bot))
