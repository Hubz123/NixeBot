# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import aiohttp
import discord
from discord.ext import commands, tasks

log = logging.getLogger("nixe.cogs.a21_youtube_wuwa_live_announce")

# ----------------------------
# Runtime toggles (runtime_env.json -> os.environ via env overlay)
# ----------------------------
ENABLE = os.getenv("NIXE_YT_WUWA_ANNOUNCE_ENABLE", "0").strip() == "1"
ANNOUNCE_CHANNEL_ID = int(os.getenv("NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID", "1453036422465585283") or "1453036422465585283")
POLL_SECONDS = int(os.getenv("NIXE_YT_WUWA_ANNOUNCE_POLL_SECONDS", "90") or "90")
CONCURRENCY = int(os.getenv("NIXE_YT_WUWA_ANNOUNCE_CONCURRENCY", "4") or "4")

ONLY_NEW_AFTER_BOOT = os.getenv("NIXE_YT_WUWA_ONLY_NEW_AFTER_BOOT", "1").strip() == "1"
BOOT_GRACE_SECONDS = int(os.getenv("NIXE_YT_WUWA_BOOT_GRACE_SECONDS", "30") or "30")
ANNOUNCE_MAX_AGE_MINUTES = int(os.getenv("NIXE_YT_WUWA_ANNOUNCE_MAX_AGE_MINUTES", "0") or "0")

WATCHLIST_PATH = os.getenv("NIXE_YT_WUWA_WATCHLIST_PATH", "data/youtube_wuwa_watchlist.json").strip() or "data/youtube_wuwa_watchlist.json"
STATE_PATH = os.getenv("NIXE_YT_WUWA_STATE_PATH", "data/youtube_wuwa_state.json").strip() or "data/youtube_wuwa_state.json"

ENV_REGEX_OVERRIDE = os.getenv("NIXE_YT_WUWA_TITLE_REGEX", "").strip()
ENV_TEMPLATE_OVERRIDE = os.getenv("NIXE_YT_WUWA_MESSAGE_TEMPLATE", "").strip()

DEFAULT_TITLE_REGEX = r"(?:#\s*)?(?:鳴潮|鸣潮)|Wuthering\s*Waves|WuWa|Wuwa|wuwa"
DEFAULT_MESSAGE_TEMPLATE = "Hey, {creator.name} just posted a new video!\n{video.link}"

# Normalize titles (brackets, fullwidth chars) to reduce regex misses.
_BRACKET_TRANS = str.maketrans({c: " " for c in "【】[]()（）「」『』〈〉《》〔〕〖〗"})
def _normalize_title(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_BRACKET_TRANS)
    # normalize hashtag variants
    s = s.replace("＃", "#")
    return s


USER_AGENT = "Mozilla/5.0 (compatible; NixeBot/1.0; +https://github.com/Hubz123/NixeBot)"

# ----------------------------
# Helpers: safe JSON IO with multiple fallback paths
# ----------------------------
def _candidate_paths(p: str) -> List[str]:
    base = pathlib.Path(__file__).resolve().parents[2]  # repo root-ish (nixe/..)
    cands = [p]
    # common fallbacks in this repo
    cands += [
        str(base / p),
        str(base / "data" / pathlib.Path(p).name),
        str(base / "nixe" / "data" / pathlib.Path(p).name),
    ]
    # de-dup
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
            log.warning("[yt-wuwa] failed to read json %s: %r", cand, e)
            continue
    return None

def _write_json_best_effort(p: str, data: Dict[str, Any]) -> None:
    for cand in _candidate_paths(p):
        try:
            os.makedirs(os.path.dirname(cand), exist_ok=True)
            with open(cand, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return
        except Exception:
            continue

# ----------------------------
# YouTube parsing
# ----------------------------
_YTIPR_RE = re.compile(r"ytInitialPlayerResponse\s*=\s*(\{.*?\});", re.DOTALL)
_YTINITDATA_RE = re.compile(r"ytInitialData\s*=\s*(\{.*?\});", re.DOTALL)

def _extract_json_blob(html: str, rx: re.Pattern) -> Optional[Dict[str, Any]]:
    m = rx.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None

def _yt_live_info(player: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], bool, Optional[datetime]]:
    """
    Returns (video_id, title, is_live_now, start_ts_utc).

    Notes:
    - We prefer isLiveNow == True when available to avoid scheduled streams and VOD spam.
    - start_ts_utc (when present) is used to suppress "already-live before bot boot" announcements.
    """
    vid = None
    title = None
    is_live_now = False
    start_ts: Optional[datetime] = None

    vd = (player.get("videoDetails") or {}) if isinstance(player, dict) else {}
    vid = vd.get("videoId")
    title = vd.get("title")
    micro = (player.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    live = micro.get("liveBroadcastDetails") or {}

    def _parse_ts(ts: Any) -> Optional[datetime]:
        if not ts or not isinstance(ts, str):
            return None
        try:
            s = ts
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            d = datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
        except Exception:
            return None

    if isinstance(live, dict):
        start_ts = _parse_ts(live.get("startTimestamp"))
        # isLiveNow is the most reliable "actually live" switch
        if "isLiveNow" in live:
            is_live_now = bool(live.get("isLiveNow"))
        else:
            # fallback: infer from timestamps (avoid upcoming/scheduled spam)
            now = datetime.now(timezone.utc)
            end_ts = _parse_ts(live.get("endTimestamp"))
            if start_ts and start_ts <= now and (not end_ts or end_ts > now):
                is_live_now = True

    if not is_live_now:
        # last resort: hlsManifestUrl strongly suggests a live stream
        sd = player.get("streamingData") or {}
        is_live_now = bool(isinstance(sd, dict) and sd.get("hlsManifestUrl"))

    return vid, title, is_live_now, start_ts

# ----------------------------
# Search resolve (name -> channelId)
# ----------------------------
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

    def key(self) -> str:
        return self.channel_id or self.url or self.handle or self.query

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

class YouTubeWuWaLiveAnnouncer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.sem = asyncio.Semaphore(max(1, CONCURRENCY))
        self.boot_time = datetime.now(timezone.utc)

        self.state: Dict[str, Any] = _read_json_any(STATE_PATH) or {}
        self.state.setdefault("announced", {})   # key -> last video_id
        self.state.setdefault("announced_vids", {})  # video_id -> unix_ts
        self.state.setdefault("resolved", {})    # query/name -> {"channel_id","title","url"}

        self.watch: Dict[str, Any] = {}
        self.targets: List[Target] = []
        self.title_rx: re.Pattern = re.compile(DEFAULT_TITLE_REGEX, re.UNICODE | re.IGNORECASE)
        self.template: str = DEFAULT_MESSAGE_TEMPLATE

        self._reload_watchlist()
        self.loop.start()

    def cog_unload(self):
        try:
            self.loop.cancel()
        except Exception:
            pass
        try:
            if self.session and not self.session.closed:
                asyncio.create_task(self.session.close())
        except Exception:
            pass

    def _reload_watchlist(self):
        cfg = _read_json_any(WATCHLIST_PATH) or {}
        self.watch = cfg

        rx_str = (ENV_REGEX_OVERRIDE or cfg.get("title_whitelist_regex") or DEFAULT_TITLE_REGEX).strip()
        tpl = (ENV_TEMPLATE_OVERRIDE or cfg.get("message_template") or DEFAULT_MESSAGE_TEMPLATE)

        try:
            self.title_rx = re.compile(rx_str, re.UNICODE | re.IGNORECASE)
        except Exception:
            self.title_rx = re.compile(DEFAULT_TITLE_REGEX, re.UNICODE | re.IGNORECASE)

        self.template = str(tpl)

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
        self.targets = out

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

    async def _http_get_text(self, url: str) -> Optional[str]:
        await self._ensure_session()
        assert self.session is not None
        async with self.sem:
            try:
                async with self.session.get(url, allow_redirects=True) as r:
                    if r.status != 200:
                        return None
                    return await r.text()
            except Exception:
                return None

    async def _resolve_channel(self, t: Target) -> Target:
        if t.channel_id or t.url or t.handle:
            return t

        cached = self.state.get("resolved", {}).get(t.query) or self.state.get("resolved", {}).get(t.name)
        if isinstance(cached, dict):
            cid = str(cached.get("channel_id") or "")
            title = str(cached.get("title") or t.name)
            url = str(cached.get("url") or "")
            if cid:
                t.channel_id = cid
            if url:
                t.url = url
            t.name = title or t.name
            if t.channel_id or t.url:
                return t

        q = quote_plus(t.query or t.name)
        search_url = f"https://www.youtube.com/results?search_query={q}&sp=EgIQAg%253D%253D"
        html = await self._http_get_text(search_url)
        if not html:
            return t
        data = _extract_json_blob(html, _YTINITDATA_RE)
        if not data:
            return t

        cand: List[Tuple[str, str]] = []
        _collect_channel_renderers(data, cand)
        best = _pick_best_channel(t.query or t.name, cand)
        if not best:
            return t

        cid, title = best
        t.channel_id = cid
        t.url = f"https://www.youtube.com/channel/{cid}"
        t.name = title or t.name

        self.state["resolved"][t.query] = {"channel_id": cid, "title": t.name, "url": t.url}
        _write_json_best_effort(STATE_PATH, self.state)
        return t

    async def _check_live(self, t: Target) -> Optional[Tuple[Target, str, str]]:
        """
        Returns (target, video_id, title) if live now and matches whitelist.
        """
        t = await self._resolve_channel(t)
        base = t.base_url()
        if not base:
            return None
        live_url = base.rstrip("/") + "/live"
        html = await self._http_get_text(live_url)
        if not html:
            return None
        player = _extract_json_blob(html, _YTIPR_RE)
        if not player:
            return None

        vid, title, is_live_now, start_ts = _yt_live_info(player)
        if not (vid and title and is_live_now):
            return None
        if not (self.title_rx.search(title) or self.title_rx.search(_normalize_title(title))):
            return None
        return t, vid, title, start_ts

    def _render_template(self, creator_name: str, video_link: str) -> str:
        msg = self.template
        msg = msg.replace("{creator.name}", creator_name)
        msg = msg.replace("{video.link}", video_link)
        return msg

    async def _post(self, channel: discord.TextChannel, creator_name: str, title: str, video_id: str):
        video_link = f"https://youtu.be/{video_id}"
        content = self._render_template(creator_name, video_link)

        embed = discord.Embed(title=title, url=video_link)
        embed.set_author(name=creator_name)
        embed.set_image(url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Watch", url=video_link))

        await channel.send(
            content=content,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tasks.loop(seconds=POLL_SECONDS)
    async def loop(self):
        # Allow live edits to JSON
        try:
            self._reload_watchlist()
        except Exception:
            pass

        if not (os.getenv("NIXE_YT_WUWA_ANNOUNCE_ENABLE", "0").strip() == "1"):
            return

        ch = self.bot.get_channel(ANNOUNCE_CHANNEL_ID)
        if not isinstance(ch, discord.TextChannel):
            return

        results = await asyncio.gather(*(self._check_live(t) for t in list(self.targets)), return_exceptions=True)
        for res in results:
            if not res or isinstance(res, Exception):
                continue
            t, vid, title, start_ts = res

            # Build stable keys to avoid duplicate posts when resolution improves (query->channel_id).
            keys: List[str] = []
            for cand in (t.channel_id, t.base_url(), t.url, t.handle, t.query, t.name):
                if cand:
                    keys.append(str(cand))
            if not keys:
                keys = [t.query]

            ann_map = self.state.setdefault("announced", {})   # key -> last video_id
            ann_vids = self.state.setdefault("announced_vids", {})  # video_id -> unix_ts (or 1)

            # Hard de-dupe by video id (covers key changes across restarts).
            if str(vid) in ann_vids or str(vid) in set(str(v) for v in ann_map.values()):
                # keep keys aligned to the vid to prevent future re-announce with a new key
                for k in keys:
                    ann_map[k] = vid
                continue

            now = datetime.now(timezone.utc)

            # Do not announce streams that started before this bot instance booted.
            if ONLY_NEW_AFTER_BOOT:
                if start_ts is None:
                    # conservative: if we cannot know start time, suppress to avoid late spam after restarts
                    for k in keys:
                        ann_map[k] = vid
                    ann_vids[str(vid)] = int(now.timestamp())
                    _write_json_best_effort(STATE_PATH, self.state)
                    log.info("[yt-wuwa] suppress (unknown start_ts) after boot: %s vid=%s", t.name, vid)
                    continue
                # Allow a small grace window for clock skew / extraction lag
                if start_ts < (self.boot_time - timedelta(seconds=max(0, BOOT_GRACE_SECONDS))):
                    for k in keys:
                        ann_map[k] = vid
                    ann_vids[str(vid)] = int(now.timestamp())
                    _write_json_best_effort(STATE_PATH, self.state)
                    age_min = int((now - start_ts).total_seconds() // 60)
                    log.info("[yt-wuwa] suppress old-live after boot: %s vid=%s age_min=%s", t.name, vid, age_min)
                    continue

            # Optional: suppress "too old" lives even without restarts (0 disables)
            if ANNOUNCE_MAX_AGE_MINUTES > 0 and start_ts is not None:
                if (now - start_ts).total_seconds() > (ANNOUNCE_MAX_AGE_MINUTES * 60):
                    for k in keys:
                        ann_map[k] = vid
                    ann_vids[str(vid)] = int(now.timestamp())
                    _write_json_best_effort(STATE_PATH, self.state)
                    age_min = int((now - start_ts).total_seconds() // 60)
                    log.info("[yt-wuwa] suppress stale-live: %s vid=%s age_min=%s", t.name, vid, age_min)
                    continue
            try:
                await self._post(ch, t.name, title, vid)
                # write to all keys to prevent key-change dupes after restart/resolve
                ann_map = self.state.setdefault("announced", {})
                ann_vids = self.state.setdefault("announced_vids", {})
                for k in keys:
                    ann_map[k] = vid
                ann_vids[str(vid)] = int(datetime.now(timezone.utc).timestamp())
                _write_json_best_effort(STATE_PATH, self.state)
                log.info("[yt-wuwa] announced live: %s vid=%s", t.name, vid)
            except Exception as e:
                log.warning("[yt-wuwa] post failed (%s): %r", t.name, e)

    @loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
        await self._ensure_session()

async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeWuWaLiveAnnouncer(bot))
