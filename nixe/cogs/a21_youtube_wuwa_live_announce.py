# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import io
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
POLL_SECONDS = int(os.getenv("NIXE_YT_WUWA_ANNOUNCE_POLL_SECONDS", "20") or "90")
CONCURRENCY = int(os.getenv("NIXE_YT_WUWA_ANNOUNCE_CONCURRENCY", "8") or "4")

ONLY_NEW_AFTER_BOOT = os.getenv("NIXE_YT_WUWA_ONLY_NEW_AFTER_BOOT", "0").strip() == "1"
BOOT_GRACE_SECONDS = int(os.getenv("NIXE_YT_WUWA_BOOT_GRACE_SECONDS", "30") or "30")
ANNOUNCE_MAX_AGE_MINUTES = int(os.getenv("NIXE_YT_WUWA_ANNOUNCE_MAX_AGE_MINUTES", "0") or "0")
DEBUG = os.getenv("NIXE_YT_WUWA_DEBUG", "0").strip() == "1"

# If enabled, let Discord generate the native YouTube embed (play button overlay).
# When disabled, Nixe uses a custom embed with a static thumbnail + "Watch" button.
ANNOUNCE_NATIVE_EMBED = os.getenv("NIXE_YT_WUWA_ANNOUNCE_NATIVE_EMBED", "1").strip() == "1"

# Optional YouTube Data API v3 key (recommended to reduce scrape flakiness)
YOUTUBE_API_KEY = (os.getenv("NIXE_YT_WUWA_YT_API_KEY", "").strip() or os.getenv("NIXE_YT_YT_API_KEY", "").strip())
YOUTUBE_API_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

WATCHLIST_PATH = (os.getenv("NIXE_YT_WUWA_WATCHLIST_PATH", "data/youtube_wuwa_watchlist.json").strip() or "data/youtube_wuwa_watchlist.json")
STATE_PATH = (os.getenv("NIXE_YT_WUWA_STATE_PATH", "data/youtube_wuwa_state.json").strip() or "data/youtube_wuwa_state.json")

# Watchlist via Discord thread (optional, but enabled by default when parent channel id is set)
WATCHLIST_PARENT_CHANNEL_ID = int(os.getenv("NIXE_YT_WUWA_WATCHLIST_PARENT_CHANNEL_ID", "1431178130155896882") or "1431178130155896882")
WATCHLIST_THREAD_NAME = os.getenv("NIXE_YT_WUWA_WATCHLIST_THREAD_NAME", "YT_WATCHLIST").strip() or "YT_WATCHLIST"
WATCHLIST_THREAD_ID_OVERRIDE = int(os.getenv("NIXE_YT_WUWA_WATCHLIST_THREAD_ID", "1453571893062926428") or "1453571893062926428")
WATCHLIST_THREAD_SCAN_LIMIT = int(os.getenv("NIXE_YT_WUWA_WATCHLIST_THREAD_SCAN_LIMIT", "200") or "200")

# Watchlist thread store message (keeps thread clean)
WATCHLIST_STORE_MARKER = "[yt-wuwa-watchlist]"
WATCHLIST_STORE_ATTACHMENT_NAME = "youtube_wuwa_watchlist.json"
WATCHLIST_CLEAN_THREAD = os.getenv("NIXE_YT_WUWA_WATCHLIST_CLEAN_THREAD", "1").strip() == "1"
WATCHLIST_STORE_MAX_HISTORY_SCAN = int(os.getenv("NIXE_YT_WUWA_WATCHLIST_STORE_MAX_HISTORY_SCAN", "50") or "50")



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

def _parse_iso_utc(ts: Any) -> Optional[datetime]:
    """Parse ISO8601 timestamps used by YouTube API into timezone-aware datetime (UTC)."""
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


# Robust extraction for YouTube embedded JSON (avoids regex truncation on nested braces)

def _extract_yt_var_json(html: str, var_name: str) -> Optional[Dict[str, Any]]:
    """Extract JSON assigned to a JS var like `ytInitialPlayerResponse = {...};`.

    Uses a balanced-brace scanner to avoid truncation on nested objects.
    """
    if not html or not var_name:
        return None
    try:
        anchor = html.find(var_name)
        if anchor < 0:
            return None
        eq = html.find('=', anchor)
        if eq < 0:
            return None
        start = html.find('{', eq)
        if start < 0:
            return None
        i = start
        depth = 0
        in_str = False
        esc = False
        while i < len(html):
            ch = html[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        blob = html[start:i+1]
                        return json.loads(blob)
            i += 1
    except Exception:
        return None
    return None
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

        self.watchlist_thread_id: int = 0
        self.watchlist_thread: Optional[discord.Thread] = None


        # Pre-seed thread id so on_message can work even before _ensure_watchlist_thread() runs.
        if WATCHLIST_THREAD_ID_OVERRIDE:
            self.watchlist_thread_id = int(WATCHLIST_THREAD_ID_OVERRIDE)
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


    @staticmethod
    def _extract_watchlist_tokens(text: str) -> List[str]:
        """Extract youtube channel handles/urls from free-form text."""
        if not text:
            return []
        toks: List[str] = []

        # normalize fullwidth symbols
        s = unicodedata.normalize("NFKC", text)

        # common URL forms
        url_rx = re.compile(r"https?://(?:www\.)?youtube\.com/[^\s>]+", re.IGNORECASE)
        for m in url_rx.finditer(s):
            u = m.group(0).strip().rstrip(').,;]')
            toks.append(u)

        # youtu.be is usually video; keep but will be filtered later
        youtu_rx = re.compile(r"https?://youtu\.be/[^\s>]+", re.IGNORECASE)
        for m in youtu_rx.finditer(s):
            u = m.group(0).strip().rstrip(').,;]')
            toks.append(u)

        # bare handles
        handle_rx = re.compile(r"(?<![\w@])@([A-Za-z0-9_\.\-]{3,})", re.IGNORECASE)
        for m in handle_rx.finditer(s):
            toks.append("@" + m.group(1))

        # de-dup while keeping order
        out: List[str] = []
        for t in toks:
            if t and t not in out:
                out.append(t)
        return out

    @classmethod
    def _token_to_target(cls, token: str) -> Optional[Dict[str, str]]:
        """Convert a token into a watchlist target dict compatible with youtube_wuwa_watchlist.json."""
        if not token:
            return None
        t = token.strip()

        handle = ""
        url = ""
        channel_id = ""

        # handle
        if t.startswith("@"):
            handle = t
            url = f"https://www.youtube.com/{handle}"
        elif "youtube.com" in t.lower():
            url = t.split("?")[0].rstrip("/")
            low = url.lower()

            # channel id
            m = re.search(r"/channel/(UC[0-9A-Za-z_\-]+)", url, re.IGNORECASE)
            if m:
                channel_id = m.group(1)

            # handle url
            m = re.search(r"/(@[0-9A-Za-z_\.\-]+)$", url, re.IGNORECASE)
            if m:
                handle = m.group(1)

            # accept only likely channel URLs
            if not ("/channel/" in low or "/@" in low or "/c/" in low or "/user/" in low):
                return None
        elif "youtu.be" in t.lower():
            # likely a video URL; skip (we only track channels)
            return None
        else:
            return None

        name = handle or channel_id or url
        query = handle or channel_id or name

        return {
            "name": name,
            "query": query,
            "handle": handle,
            "channel_id": channel_id,
            "url": url,
        }

    @classmethod
    def _merge_targets(cls, existing: List[Any], new_targets: List[Dict[str, str]]) -> Tuple[List[Any], int, List[Dict[str, str]]]:
        """Merge target dicts into existing targets list (skip dupes).

        Returns:
          merged_list, added_count, added_items
        """
        def norm_handle(h: str) -> str:
            return (h or "").strip().lower()

        def norm_url(u: str) -> str:
            return (u or "").strip().rstrip("/").lower()

        def norm_cid(c: str) -> str:
            return (c or "").strip()

        seen_h = set()
        seen_u = set()
        seen_c = set()

        for item in existing or []:
            if isinstance(item, dict):
                seen_h.add(norm_handle(str(item.get("handle") or "")))
                seen_u.add(norm_url(str(item.get("url") or "")))
                seen_c.add(norm_cid(str(item.get("channel_id") or "")))
            elif isinstance(item, str):
                s = item.strip()
                if s.startswith("@"):
                    seen_h.add(norm_handle(s))
                elif "youtube.com" in s.lower():
                    seen_u.add(norm_url(s))

        merged = list(existing or [])
        added = 0
        added_items: List[Dict[str, str]] = []

        for t in new_targets:
            h = norm_handle(t.get("handle", ""))
            u = norm_url(t.get("url", ""))
            c = norm_cid(t.get("channel_id", ""))

            is_dupe = False
            if h and h in seen_h:
                is_dupe = True
            if u and u in seen_u:
                is_dupe = True
            if c and c in seen_c:
                is_dupe = True

            if is_dupe:
                continue

            merged.append(t)
            added_items.append(t)
            added += 1
            if h:
                seen_h.add(h)
            if u:
                seen_u.add(u)
            if c:
                seen_c.add(c)

        return merged, added, added_items

    async def _ensure_watchlist_thread(self) -> Optional[discord.Thread]:
        """Ensure a public thread exists under WATCHLIST_PARENT_CHANNEL_ID and return it."""
        # If overridden, just fetch it.
        if WATCHLIST_THREAD_ID_OVERRIDE:
            try:
                ch = self.bot.get_channel(WATCHLIST_THREAD_ID_OVERRIDE) or await self.bot.fetch_channel(WATCHLIST_THREAD_ID_OVERRIDE)
                if isinstance(ch, discord.Thread):
                    self.watchlist_thread_id = ch.id
                    self.watchlist_thread = ch
                    return ch
            except Exception:
                pass

        if not WATCHLIST_PARENT_CHANNEL_ID:
            return None

        # Fetch parent channel
        parent = self.bot.get_channel(WATCHLIST_PARENT_CHANNEL_ID)
        if parent is None:
            try:
                parent = await self.bot.fetch_channel(WATCHLIST_PARENT_CHANNEL_ID)
            except Exception:
                parent = None

        if not isinstance(parent, discord.TextChannel):
            return None

        target_name = WATCHLIST_THREAD_NAME.strip()

        # Search active threads
        try:
            for th in getattr(parent, "threads", []) or []:
                if isinstance(th, discord.Thread) and th.name == target_name:
                    try:
                        if th.archived:
                            await th.edit(archived=False, locked=False)
                    except Exception:
                        pass
                    self.watchlist_thread_id = th.id
                    self.watchlist_thread = th
                    return th
        except Exception:
            pass

        # Search archived public threads (best-effort)
        try:
            async for th in parent.archived_threads(limit=50, private=False):
                if isinstance(th, discord.Thread) and th.name == target_name:
                    try:
                        await th.edit(archived=False, locked=False)
                    except Exception:
                        pass
                    self.watchlist_thread_id = th.id
                    self.watchlist_thread = th
                    return th
        except Exception:
            pass

        # Create a new thread by creating a starter message
        try:
            starter = await parent.send(
                "[yt-wuwa] Watchlist thread auto-created. Paste YouTube channel links/handles here (e.g., @handle or https://www.youtube.com/@handle).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            th = await starter.create_thread(
                name=target_name,
                auto_archive_duration=10080,  # 7 days
                reason="yt-wuwa watchlist",
            )
            self.watchlist_thread_id = th.id
            self.watchlist_thread = th
            return th
        except Exception as e:
            log.warning("[yt-wuwa] failed to create watchlist thread: %r", e)
            return None

    async def _bootstrap_watchlist_from_thread(self):
        """Scan recent messages in the watchlist thread and merge into local watchlist json."""
        th = await self._ensure_watchlist_thread()
        if not th:
            return

        cfg = _read_json_any(WATCHLIST_PATH) or {}

        # Prefer canonical store attachment (survives restarts even if local disk is wiped).
        store_cfg: Optional[Dict[str, Any]] = None
        try:
            store_cfg = await self._load_watchlist_from_store_attachment(th)
        except Exception:
            store_cfg = None
        if isinstance(store_cfg, dict) and store_cfg.get("targets"):
            try:
                merged, _, _ = self._merge_targets(cfg.get("targets") or [], store_cfg.get("targets") or [])
                cfg["targets"] = merged
                _write_json_best_effort(WATCHLIST_PATH, cfg)
                self._reload_watchlist()
            except Exception:
                pass

        existing_targets = cfg.get("targets") or []
        new_targets: List[Dict[str, str]] = []

        try:
            async for msg in th.history(limit=max(10, WATCHLIST_THREAD_SCAN_LIMIT), oldest_first=True):
                if not msg or not getattr(msg, "content", ""):
                    continue
                if msg.author and msg.author.bot:
                    continue
                for tok in self._extract_watchlist_tokens(msg.content):
                    td = self._token_to_target(tok)
                    if td:
                        new_targets.append(td)
        except Exception as e:
            log.warning("[yt-wuwa] watchlist thread history scan failed: %r", e)
            return

        merged, added, _added_items = self._merge_targets(existing_targets, new_targets)
        if added > 0:
            cfg.setdefault("enabled", True)
            cfg["targets"] = merged
            _write_json_best_effort(WATCHLIST_PATH, cfg)
            log.info("[yt-wuwa] watchlist updated from thread: +%d targets", added)

        # Reload in-memory list
        self._reload_watchlist()

    def _brief_target(self, t: Dict[str, str]) -> str:
        name = (t.get("name") or t.get("channel_name") or "").strip()
        handle = (t.get("handle") or "").strip()
        url = (t.get("url") or "").strip()
        cid = (t.get("channel_id") or "").strip()
        ident = handle or url or cid or "unknown"
        if name:
            return f"{name} ({ident})"
        return ident

    def _summarize_targets(self, items: List[Dict[str, str]], limit: int = 6) -> str:
        if not items:
            return ""
        parts = [self._brief_target(x) for x in items[:limit]]
        if len(items) > limit:
            parts.append(f"+{len(items) - limit} more")
        return ", ".join(parts)

    async def _try_fetch_channel_name_oembed(self, url: str) -> Optional[str]:
        """Best-effort channel name resolution for logging/UI.

        Uses YouTube oEmbed to retrieve author_name. If it fails, returns None.
        """
        if not url:
            return None
        try:
            await self._ensure_session()
            oembed_url = f"https://www.youtube.com/oembed?url={quote_plus(url)}&format=json"
            async with self.session.get(oembed_url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                author = (data.get("author_name") or "").strip()
                return author or None
        except Exception:
            return None


    def _format_watchlist_entry(self, t: Dict[str, str]) -> str:
        name = (t.get("name") or t.get("channel_name") or "").strip()
        handle = (t.get("handle") or "").strip()
        url = (t.get("url") or "").strip()
        cid = (t.get("channel_id") or "").strip()

        ident = handle or (url or "") or (cid or "")
        if name and ident:
            return f"{name} — {ident}"
        return name or ident or "unknown"

    def _build_watchlist_embed(self, targets: List[Dict[str, str]]) -> discord.Embed:
        # Keep it in ONE embed as requested; truncate if somehow exceeds limits.
        lines: List[str] = []
        for i, t in enumerate(targets, start=1):
            lines.append(f"{i}. {self._format_watchlist_entry(t)}")
        desc = "\n".join(lines)
        if len(desc) > 4000:
            # Hard cap to keep Discord embed safe; still one embed.
            desc = desc[:3980] + "\n… (truncated)"
        emb = discord.Embed(
            title="YouTube WuWa Watchlist",
            description=desc or "(empty)",
        )
        emb.set_footer(text=f"{len(targets)} channel(s) • Auto-synced from thread")
        return emb


    def _build_watchlist_attachment_bytes(self, cfg: Dict[str, Any]) -> bytes:
        """Serialize watchlist cfg to bytes for Discord attachment (persistent across restarts)."""
        try:
            payload = json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
            return payload
        except Exception:
            try:
                return b"{}"
            except Exception:
                return b"{}"

    async def _load_watchlist_from_store_attachment(self, th: discord.Thread) -> Optional[Dict[str, Any]]:
        """Best-effort: read canonical watchlist cfg from the bot store message attachment."""
        try:
            mid = int(self.state.get("watchlist_store_mid") or 0)
            msg: Optional[discord.Message] = None
            if mid:
                try:
                    msg = await th.fetch_message(mid)
                except Exception:
                    msg = None

            # Fallback: scan recent messages for the marker
            if msg is None:
                async for m in th.history(limit=max(20, WATCHLIST_STORE_MAX_HISTORY_SCAN), oldest_first=False):
                    if not m or not (m.author and self.bot.user and m.author.id == self.bot.user.id):
                        continue
                    if (m.content or "").strip().startswith(WATCHLIST_STORE_MARKER):
                        msg = m
                        self.state["watchlist_store_mid"] = m.id
                        _write_json_best_effort(STATE_PATH, self.state)
                        break

            if msg is None:
                return None

            # Prefer JSON attachment if present
            atts = list(getattr(msg, "attachments", []) or [])
            for a in atts:
                try:
                    if (a.filename or "").lower() == WATCHLIST_STORE_ATTACHMENT_NAME.lower():
                        raw = await a.read()
                        obj = json.loads(raw.decode("utf-8", errors="replace"))
                        if isinstance(obj, dict):
                            return obj
                except Exception:
                    continue

            # Fallback: try parse embed description (non-authoritative, but better than nothing)
            try:
                if msg.embeds:
                    desc = (msg.embeds[0].description or "")
                    targets: List[Dict[str, str]] = []
                    for line in desc.splitlines():
                        line = line.strip()
                        if not line or not re.match(r"^\d+\.", line):
                            continue
                        # "1. Name — @handle" / "1. Name — url"
                        line = re.sub(r"^\d+\.\s*", "", line)
                        parts = [p.strip() for p in line.split("—", 1)]
                        name = parts[0].strip() if parts else ""
                        ident = parts[1].strip() if len(parts) > 1 else ""
                        t: Dict[str, str] = {"name": name, "query": name}
                        if ident.startswith("@"):
                            t["handle"] = ident
                            t["url"] = f"https://www.youtube.com/{ident}"
                        elif ident.startswith("http"):
                            t["url"] = ident
                        targets.append(t)
                    if targets:
                        cfg = _read_json_any(WATCHLIST_PATH) or {}
                        cfg["targets"] = targets
                        return cfg
            except Exception:
                pass

            return None
        except Exception:
            return None

    async def _find_or_create_watchlist_store_message(self, th: discord.Thread) -> Optional[discord.Message]:
        if not th:
            return None

        mid = int(self.state.get("watchlist_store_mid") or 0)
        if mid:
            try:
                m = await th.fetch_message(mid)
                if m and m.author and self.bot.user and m.author.id == self.bot.user.id:
                    return m
            except Exception:
                pass

        # Scan recent messages for marker
        try:
            async for m in th.history(limit=WATCHLIST_STORE_MAX_HISTORY_SCAN, oldest_first=False):
                if not m:
                    continue
                if not (m.author and self.bot.user and m.author.id == self.bot.user.id):
                    continue
                if (m.content or "").strip().startswith(WATCHLIST_STORE_MARKER):
                    self.state["watchlist_store_mid"] = m.id
                    _write_json_best_effort(STATE_PATH, self.state)
                    return m
        except Exception:
            pass

        # Create new store message
        try:
            emb = self._build_watchlist_embed(self.watch.get("targets") or [])
            cfg = _read_json_any(WATCHLIST_PATH) or {}
            payload = self._build_watchlist_attachment_bytes(cfg)
            fp = io.BytesIO(payload)
            file = discord.File(fp=fp, filename=WATCHLIST_STORE_ATTACHMENT_NAME)
            m = await th.send(WATCHLIST_STORE_MARKER, embed=emb, file=file, allowed_mentions=discord.AllowedMentions.none())
            self.state["watchlist_store_mid"] = m.id
            _write_json_best_effort(STATE_PATH, self.state)
            return m
        except Exception as e:
            log.warning("[yt-wuwa] failed to create watchlist store message: %r", e)
            return None

    async def _sync_watchlist_store_message(self, th: Optional[discord.Thread] = None) -> None:
        # Read from JSON (single source of truth for targets), then render to the thread embed.
        try:
            if th is None:
                th = await self._ensure_watchlist_thread()
            if not th:
                return
            cfg = _read_json_any(WATCHLIST_PATH) or {}
            targets = cfg.get("targets") or []
            # stable sort for readability: by name then handle/url
            def _k(x: Dict[str, str]) -> str:
                return ((x.get("name") or "") + "|" + (x.get("handle") or "") + "|" + (x.get("url") or "")).lower()
            try:
                targets = sorted(list(targets), key=_k)
            except Exception:
                targets = list(targets)

            store = await self._find_or_create_watchlist_store_message(th)
            if not store:
                return
            emb = self._build_watchlist_embed(targets)

            # Persist canonical watchlist into the store message attachment so it survives restarts / ephemeral disk.
            try:
                cfg_out = dict(cfg)
                cfg_out["targets"] = list(targets)
            except Exception:
                cfg_out = {"targets": list(targets)}
            payload = self._build_watchlist_attachment_bytes(cfg_out)
            fp = io.BytesIO(payload)
            file = discord.File(fp=fp, filename=WATCHLIST_STORE_ATTACHMENT_NAME)

            try:
                # discord.py 2.x supports replacing attachments via Message.edit(attachments=[...])
                await store.edit(content=WATCHLIST_STORE_MARKER, embed=emb, attachments=[file], allowed_mentions=discord.AllowedMentions.none())
            except TypeError:
                # Fallback: cannot edit attachments; create a new store message (do NOT delete the old one).
                m2 = await th.send(WATCHLIST_STORE_MARKER, embed=emb, file=file, allowed_mentions=discord.AllowedMentions.none())
                self.state["watchlist_store_mid"] = m2.id
                _write_json_best_effort(STATE_PATH, self.state)

                # Archive the previous store message in-place (keep it for safety; never delete thread history).
                try:
                    await store.edit(
                        content=f"{WATCHLIST_STORE_MARKER} (archived; superseded by {m2.id})",
                        embed=None,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except TypeError:
                    try:
                        await store.edit(
                            content=f"{WATCHLIST_STORE_MARKER} (archived; superseded by {m2.id})",
                            embeds=[],
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except Exception:
                        pass
                except Exception:
                    pass

                # Best-effort: pin the new canonical store message so it is easy to find.
                try:
                    await m2.pin(reason="watchlist store (canonical)")
                except Exception:
                    pass
        except Exception as e:
            log.warning("[yt-wuwa] watchlist store sync failed: %r", e)


    async def _enrich_watchlist_names(self, items: List[Dict[str, str]]) -> None:
        # Best-effort: resolve channel display names for newly added items so the embed/logs are clearer.
        if not items:
            return
        try:
            cfg = _read_json_any(WATCHLIST_PATH) or {}
            targets = cfg.get("targets") or []
            changed = False

            def _norm(x: str) -> str:
                return (x or "").strip().rstrip("/").lower()

            for it in items:
                url = _norm(it.get("url") or "")
                handle = (it.get("handle") or "").strip().lower()
                cid = (it.get("channel_id") or "").strip()
                if not url and handle:
                    url = _norm(f"https://www.youtube.com/{handle}")
                if not url and cid:
                    url = _norm(f"https://www.youtube.com/channel/{cid}")

                if not url:
                    continue

                nm = await self._try_fetch_channel_name_oembed(url)
                if not nm:
                    continue

                for t in targets:
                    if not isinstance(t, dict):
                        continue
                    t_url = _norm(t.get("url") or "")
                    t_handle = (t.get("handle") or "").strip().lower()
                    t_cid = (t.get("channel_id") or "").strip()
                    if (url and t_url and url == t_url) or (handle and t_handle and handle == t_handle) or (cid and t_cid and cid == t_cid):
                        cur = (t.get("name") or "").strip()
                        # Fill if missing or too generic
                        if (not cur) or (handle and cur.strip().lower() == handle) or (cur.startswith("@") and cur.lower() == handle):
                            t["name"] = nm
                            t["query"] = nm
                            changed = True

            if changed:
                cfg["targets"] = targets
                _write_json_best_effort(WATCHLIST_PATH, cfg)
        except Exception:
            return

    
async def _cleanup_watchlist_thread(self, th: discord.Thread, keep_mid: int) -> None:
    # Best-effort: keep the store message (memory) intact.
    # Only delete moderator "add" messages (youtube links / handles). Never delete the store message.
    if not WATCHLIST_CLEAN_THREAD:
        return
    if not th or not keep_mid:
        return
    try:
        async for m in th.history(limit=WATCHLIST_THREAD_SCAN_LIMIT, oldest_first=False):
            if not m or m.id == keep_mid:
                continue
            if getattr(m, "pinned", False):
                continue
            # Never delete our own messages (safest).
            if m.author and self.bot.user and m.author.id == self.bot.user.id:
                continue
            txt = (getattr(m, "content", "") or "").strip()
            if not txt:
                continue
            low = txt.lower()
            looks_like_add = ("youtube.com" in low) or ("youtu.be" in low) or ("/@" in low) or txt.startswith("@")
            if not looks_like_add:
                continue
            try:
                await m.delete()
            except Exception:
                pass
    except Exception:
        pass

    async def _ingest_watchlist_message(self, text: str) -> Tuple[int, List[Dict[str, str]]]:
        """Parse a single message and merge any new targets."""
        toks = self._extract_watchlist_tokens(text)
        if not toks:
            return 0, []
        new_targets: List[Dict[str, str]] = []
        for tok in toks:
            td = self._token_to_target(tok)
            if td:
                new_targets.append(td)
        if not new_targets:
            return 0, []

        cfg = _read_json_any(WATCHLIST_PATH) or {}
        existing_targets = cfg.get("targets") or []
        merged, added, added_items = self._merge_targets(existing_targets, new_targets)
        if added <= 0:
            return 0, []

        cfg.setdefault("enabled", True)
        # Best-effort: resolve channel name for new additions (for clearer logs/UI).
        # This does not affect matching/dedup; it is purely informational.
        for t in added_items:
            if isinstance(t, dict) and not (t.get("name") or t.get("channel_name")):
                url = (t.get("url") or "").strip()
                if not url and (t.get("handle") or "").strip().startswith("@"):
                    url = f"https://www.youtube.com/{t.get('handle').strip()}"
                nm = await self._try_fetch_channel_name_oembed(url)
                if nm:
                    t["name"] = nm

        cfg["targets"] = merged
        _write_json_best_effort(WATCHLIST_PATH, cfg)
        self._reload_watchlist()
        return added, added_items
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Auto-ingest watchlist additions from the dedicated thread, keep the thread clean,
        # and keep a single embed up-to-date as the canonical list.
        try:
            if not message or not getattr(message, "channel", None):
                return

            # Ignore bot messages (including our own store message)
            if message.author and getattr(message.author, "bot", False):
                return

            if not self.watchlist_thread_id:
                # Fallback to configured override if not yet cached
                if WATCHLIST_THREAD_ID_OVERRIDE:
                    self.watchlist_thread_id = int(WATCHLIST_THREAD_ID_OVERRIDE)
                else:
                    return
            if getattr(message.channel, "id", 0) != self.watchlist_thread_id:
                return

            # Collect text from message content and small text/json attachments
            texts: List[str] = []
            content = (getattr(message, "content", "") or "").strip()
            if content:
                texts.append(content)

            atts = getattr(message, "attachments", None) or []
            for att in atts:
                try:
                    name = (getattr(att, "filename", "") or "").lower()
                    size = int(getattr(att, "size", 0) or 0)
                    if size <= 0 or size > 250_000:
                        continue
                    if not (name.endswith(".txt") or name.endswith(".json")):
                        continue
                    b = await att.read()
                    if b:
                        texts.append(b.decode("utf-8", errors="ignore"))
                except Exception:
                    continue

            if not texts:
                # still try to delete to keep thread clean
                try:
                    await message.delete()
                except Exception:
                    pass
                return

            added_total = 0
            added_items_all: List[Dict[str, str]] = []
            for t in texts:
                added, added_items = await self._ingest_watchlist_message(t)
                if added > 0:
                    added_total += added
                    added_items_all.extend(list(added_items or []))

            if added_total > 0:
                # Enrich names (best-effort) so logs + embed show channel names.
                await self._enrich_watchlist_names(added_items_all)
                log.info("[yt-wuwa] watchlist ingest: +%d targets: %s (thread=%s)",
                         added_total, self._summarize_targets(added_items_all), self.watchlist_thread_id)

            # Always refresh store embed and keep the thread clean
            try:
                th = await self._ensure_watchlist_thread()
                if th:
                    await self._sync_watchlist_store_message(th)
                    store_mid = int(self.state.get("watchlist_store_mid") or 0)
                    if store_mid:
                        await self._cleanup_watchlist_thread(th, store_mid)
            except Exception:
                pass

            # Finally delete the moderator message so the thread only keeps the store embed
            try:
                await message.delete()
            except Exception:
                pass

        except Exception as e:
            log.warning("[yt-wuwa] watchlist ingest failed: %r", e)


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
                        if DEBUG:
                            try:
                                body = await r.text()
                            except Exception:
                                body = ''
                            log.warning('[yt-wuwa] http %s %s status=%s body=%s', r.method, url, r.status, body[:200])
                        return None
                    return await r.text()
            except Exception:
                return None

    async def _yt_api_videos(self, video_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch video metadata via YouTube Data API v3 (videos.list). Returns id->item."""
        if not YOUTUBE_API_KEY:
            return {}
        ids = [vid for vid in (video_ids or []) if isinstance(vid, str) and len(vid) == 11]
        if not ids:
            return {}
        # videos.list supports up to 50 ids per request.
        out: Dict[str, Dict[str, Any]] = {}
        await self._ensure_session()
        assert self.session is not None
        for i in range(0, len(ids), 50):
            chunk = ids[i:i+50]
            params = {
                "part": "snippet,liveStreamingDetails",
                "id": ",".join(chunk),
                "key": YOUTUBE_API_KEY,
            }
            try:
                async with self.sem:
                    async with self.session.get(YOUTUBE_API_VIDEOS_URL, params=params) as r:
                        txt = await r.text()
                        if r.status != 200:
                            if DEBUG:
                                log.warning("[yt-wuwa] yt-api videos.list status=%s body=%s", r.status, txt[:300])
                            continue
                        data = json.loads(txt)
                        for item in (data.get("items") or []):
                            vid = str(item.get("id") or "")
                            if vid:
                                out[vid] = item
            except Exception as e:
                if DEBUG:
                    log.warning("[yt-wuwa] yt-api videos.list error: %r", e)
                continue
        return out

    def _extract_video_id_fallback(self, html: str) -> Optional[str]:
        """Best-effort extraction of a videoId from a /live page HTML when JSON parsing fails."""
        if not html:
            return None
        # Try to find a videoId near isLiveNow/hlsManifestUrl markers.
        markers = ['"isLiveNow":true', '"hlsManifestUrl"', '"isLiveContent":true']
        for mk in markers:
            pos = html.find(mk)
            if pos != -1:
                window = html[max(0, pos-5000):pos+500]
                vids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', window)
                if vids:
                    return vids[-1]
        # Fallback: first videoId in document.
        m = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
        if m:
            return m.group(1)
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
        data = _extract_yt_var_json(html, 'ytInitialData') or _extract_json_blob(html, _YTINITDATA_RE)
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

        # Fast path: parse ytInitialPlayerResponse (scrape)
        player = _extract_yt_var_json(html, 'ytInitialPlayerResponse') or _extract_json_blob(html, _YTIPR_RE)
        if player:
            vid, title, is_live_now, start_ts = _yt_live_info(player)
            if not (vid and title and is_live_now):
                return None
        else:
            # Fallback path: extract videoId from HTML, then verify via YouTube Data API (low quota, 1 unit).
            vid = self._extract_video_id_fallback(html)
            if not vid:
                return None
            api_map = await self._yt_api_videos([vid])
            item = api_map.get(vid) if isinstance(api_map, dict) else None
            if not item:
                return None
            snippet = item.get("snippet") or {}
            lsd = item.get("liveStreamingDetails") or {}
            title = str(snippet.get("title") or "")
            # Determine live-now from liveStreamingDetails
            actual_start = lsd.get("actualStartTime")
            actual_end = lsd.get("actualEndTime")
            is_live_now = bool(actual_start) and not bool(actual_end)
            start_ts = _parse_iso_utc(actual_start) if actual_start else None
            if not (title and is_live_now):
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
        # Use the canonical watch URL so Discord is more likely to render the native YouTube player-style embed.
        video_link = f"https://www.youtube.com/watch?v={video_id}"
        content = self._render_template(creator_name, video_link)

        # Native embed mode: do NOT attach a custom embed/image; let Discord unfurl the YouTube link.
        if ANNOUNCE_NATIVE_EMBED:
            try:
                await channel.send(
                    content=content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as e:
                # Never let the announce loop die due to a send error.
                if DEBUG:
                    log.warning("[yt-wuwa] send failed (native): %r", e)
                raise
            return

        # Custom embed mode (legacy): static thumbnail + link button.
        embed = discord.Embed(title=title, url=video_link)
        embed.set_author(name=creator_name)
        embed.set_image(url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Watch", url=video_link))

        try:
            await channel.send(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            # Never let the announce loop die due to a send error.
            if DEBUG:
                log.warning("[yt-wuwa] send failed: %r", e)
            raise


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
            try:
                ch = await self.bot.fetch_channel(ANNOUNCE_CHANNEL_ID)
            except Exception:
                return
        if not isinstance(ch, discord.TextChannel):
            return

        # Run checks with per-target timeout and a hard loop deadline to avoid multi-minute stalls.

        async def _run_one(tt):

            try:

                return await asyncio.wait_for(self._check_live(tt), timeout=CHECK_TIMEOUT_SECONDS)

            except Exception as e:

                if DEBUG:

                    try:

                        q = tt.get('query') if isinstance(tt, dict) else 'unknown'

                    except Exception:

                        q = 'unknown'

                    log.info('[yt-wuwa] check timeout/err for %s: %r', q, e)

                return None


        tasks_list = [asyncio.create_task(_run_one(t)) for t in list(self.targets)]

        done, pending = await asyncio.wait(tasks_list, timeout=LOOP_DEADLINE_SECONDS)

        for p in pending:

            p.cancel()


        results = []

        for d in done:

            if d.cancelled():

                continue

            try:

                r = d.result()

            except Exception:

                continue

            if r:

                results.append(r)

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
                    # YouTube sometimes omits startTimestamp even while live; do not skip.
                    # Treat as 'new enough' and announce once (hard de-dupe still applies).
                    start_ts = now
                    log.info("[yt-wuwa] start_ts missing; allow announce (treated as now): %s vid=%s", t.name, vid)
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
                delay_min = None
                if start_ts is not None:
                    try:
                        delay_min = int((now - start_ts).total_seconds() // 60)
                    except Exception:
                        delay_min = None
                if delay_min is None:
                    log.info("[yt-wuwa] announced live: %s vid=%s", t.name, vid)
                else:
                    log.info("[yt-wuwa] announced live: %s vid=%s delay_min=%s", t.name, vid, delay_min)
            except Exception as e:
                log.warning("[yt-wuwa] post failed (%s): %r", t.name, e)

    @loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
        await self._ensure_session()

        # Seed local watchlist from the persistent store message attachment before doing any cleanup.
        try:
            th0 = await self._ensure_watchlist_thread()
            if th0:
                sc0 = await self._load_watchlist_from_store_attachment(th0)
                if isinstance(sc0, dict) and sc0.get("targets"):
                    _write_json_best_effort(WATCHLIST_PATH, sc0)
                    self._reload_watchlist()
        except Exception:
            pass
        try:
            await self._bootstrap_watchlist_from_thread()
        except Exception as e:
            log.warning("[yt-wuwa] watchlist bootstrap failed: %r", e)

        # Ensure the canonical watchlist embed exists immediately after boot.
        try:
            th = await self._ensure_watchlist_thread()
            if th:
                await self._sync_watchlist_store_message(th)
                store_mid = int(self.state.get("watchlist_store_mid") or 0)
                if store_mid:
                    await self._cleanup_watchlist_thread(th, store_mid)
        except Exception as e:
            log.warning("[yt-wuwa] watchlist store sync failed: %r", e)


async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeWuWaLiveAnnouncer(bot))
