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
import html as _html
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse, urlunparse

import aiohttp
import discord
from discord.ext import commands, tasks

log = logging.getLogger("nixe.cogs.a21_youtube_wuwa_live_announce")

# ---------------------------------------------------------------------------
# Cross-task de-dupe guard
# Prevents duplicate announce posts when the loop is accidentally started twice
# (e.g., cog double-load) or when overlapping targets resolve to the same video.
# ---------------------------------------------------------------------------
_ANNOUNCE_LOCK = asyncio.Lock()
_INFLIGHT_VIDS: set[str] = set()

# ---------------------------------------------------------------------------
# Persistent watchlist pager (optional)
# If something goes wrong, the cog falls back to "no view" and still works.
# ---------------------------------------------------------------------------
_WATCHLIST_BTN_PREV = "ytwuwa:watchlist:prev"
_WATCHLIST_BTN_NEXT = "ytwuwa:watchlist:next"


class _YTWatchlistPager(discord.ui.View):
    """Persistent Prev/Next buttons for the watchlist store message.

    Notes:
      - Uses stable custom_id so it continues to work after restart
      - Computes current page from the message embed footer
      - Loads targets from the store attachment first (best), then falls back to WATCHLIST_PATH
    """

    def __init__(self, cog: "YouTubeWuWaLiveAnnouncer", page: int = 1, total_pages: int = 1):
        super().__init__(timeout=None)
        self.cog = cog
        self.page = max(1, int(page or 1))
        self.total_pages = max(1, int(total_pages or 1))

        prev_disabled = self.page <= 1
        next_disabled = self.page >= self.total_pages

        prev_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Prev",
            custom_id=_WATCHLIST_BTN_PREV,
            disabled=prev_disabled,
        )
        next_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Next",
            custom_id=_WATCHLIST_BTN_NEXT,
            disabled=next_disabled,
        )

        prev_btn.callback = self._on_prev  # type: ignore[attr-defined]
        next_btn.callback = self._on_next  # type: ignore[attr-defined]

        self.add_item(prev_btn)
        self.add_item(next_btn)

    @staticmethod
    def _parse_page_footer(msg: Optional[discord.Message]) -> Tuple[int, int]:
        try:
            if not msg or not getattr(msg, "embeds", None):
                return 1, 1
            emb = msg.embeds[0]
            ft = (getattr(getattr(emb, "footer", None), "text", "") or "").strip()
            # expected: "Page x/y • ..." but tolerate variants
            m = re.search(r"(?:page\s*)?(\d+)\s*/\s*(\d+)", ft, re.IGNORECASE)
            if not m:
                return 1, 1
            return max(1, int(m.group(1))), max(1, int(m.group(2)))
        except Exception:
            return 1, 1

    async def _load_targets_from_message(self, msg: Optional[discord.Message]) -> List[Dict[str, str]]:
        # Prefer JSON attachment on the store message (survives restarts / ephemeral disk).
        try:
            if msg:
                atts = list(getattr(msg, "attachments", []) or [])
                for a in atts:
                    try:
                        if (a.filename or "").lower() == WATCHLIST_STORE_ATTACHMENT_NAME.lower():
                            raw = await a.read()
                            obj = json.loads(raw.decode("utf-8", errors="replace"))
                            if isinstance(obj, dict):
                                t = obj.get("targets") or []
                                if isinstance(t, list):
                                    return list(t)
                    except Exception:
                        continue
        except Exception:
            pass

        # Fallback: local JSON (if present)
        try:
            cfg = _read_json_any(WATCHLIST_PATH) or {}
            t = cfg.get("targets") or []
            if isinstance(t, list):
                return list(t)
        except Exception:
            pass
        return []

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        try:
            msg = getattr(interaction, "message", None)
            cur_page, _ = self._parse_page_footer(msg)
            targets = await self._load_targets_from_message(msg)

            # Recompute total pages from targets for correctness.
            try:
                merged, _, _ = self.cog._merge_targets([], targets or [])
                total = len([t for t in merged if isinstance(t, dict)])
            except Exception:
                total = len(targets or [])

            page_size = 60
            total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
            new_page = max(1, min(total_pages, int(cur_page) + int(delta)))

            emb = self.cog._build_watchlist_embed(targets, page=new_page)
            view = self.cog._build_watchlist_view_for_targets(targets, page=new_page)

            try:
                await interaction.response.edit_message(embed=emb, view=view)
            except discord.InteractionResponded:
                await interaction.edit_original_response(embed=emb, view=view)
        except Exception:
            try:
                # best-effort: do not spam; just ack silently if possible
                if interaction and interaction.response and not interaction.response.is_done():
                    await interaction.response.defer()
            except Exception:
                pass

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, -1)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, +1)



def _env_int(name: str, default: int) -> int:
    """Parse integer env var robustly.
    - Missing or empty => default
    - Non-integer => default
    """
    try:
        v = os.getenv(name)
        if v is None:
            return int(default)
        v = str(v).strip()
        if not v:
            return int(default)
        return int(v)
    except Exception:
        return int(default)

# ----------------------------
# Runtime toggles (runtime_env.json -> os.environ via env overlay)
# ----------------------------
ENABLE = os.getenv("NIXE_YT_WUWA_ANNOUNCE_ENABLE", "0").strip() == "1"
ANNOUNCE_CHANNEL_ID = _env_int("NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID", 1453036422465585283)
POLL_SECONDS = _env_int("NIXE_YT_WUWA_ANNOUNCE_POLL_SECONDS", 20)
CONCURRENCY = _env_int("NIXE_YT_WUWA_ANNOUNCE_CONCURRENCY", 8)
NOTIFY_ROLE_ID = _env_int("NIXE_YT_WUWA_NOTIFY_ROLE_ID", 0)
def _env_float(key: str, default: float) -> float:
    try:
        raw = os.getenv(key, "").strip()
        if not raw:
            return float(default)
        return float(raw)
    except Exception:
        return float(default)

# Per-target check timeout and overall loop deadline (seconds)
CHECK_TIMEOUT_SECONDS = _env_float("NIXE_YT_WUWA_CHECK_TIMEOUT_SECONDS", 15.0)
# Default deadline: just under the poll interval, but never less than 5 seconds.
LOOP_DEADLINE_SECONDS = _env_float(
    "NIXE_YT_WUWA_LOOP_DEADLINE_SECONDS",
    max(5.0, float(POLL_SECONDS) - 2.0),
)



ONLY_NEW_AFTER_BOOT = os.getenv("NIXE_YT_WUWA_ONLY_NEW_AFTER_BOOT", "0").strip() == "1"
BOOT_GRACE_SECONDS = _env_int("NIXE_YT_WUWA_BOOT_GRACE_SECONDS", 30)
ANNOUNCE_MAX_AGE_MINUTES = _env_int("NIXE_YT_WUWA_ANNOUNCE_MAX_AGE_MINUTES", 0)
DEBUG = os.getenv("NIXE_YT_WUWA_DEBUG", "0").strip() == "1"

# If enabled, let Discord generate the native YouTube embed (play button overlay).
# When disabled, Nixe uses a custom embed with a static thumbnail + "Watch" button.
ANNOUNCE_NATIVE_EMBED = os.getenv("NIXE_YT_WUWA_ANNOUNCE_NATIVE_EMBED", "1").strip() == "1"

# Optional YouTube API v3 key (recommended to reduce scrape flakiness)
YOUTUBE_API_KEY = ""  # hard-disabled: do not use YouTube API
WATCHLIST_PATH = (os.getenv("NIXE_YT_WUWA_WATCHLIST_PATH", "data/youtube_wuwa_watchlist.json").strip() or "data/youtube_wuwa_watchlist.json")
STATE_PATH = (os.getenv("NIXE_YT_WUWA_STATE_PATH", "data/youtube_wuwa_state.json").strip() or "data/youtube_wuwa_state.json")

# Watchlist via Discord thread (optional, but enabled by default when parent channel id is set)
WATCHLIST_PARENT_CHANNEL_ID = _env_int("NIXE_YT_WUWA_WATCHLIST_PARENT_CHANNEL_ID", 1431178130155896882)
WATCHLIST_THREAD_NAME = os.getenv("NIXE_YT_WUWA_WATCHLIST_THREAD_NAME", "YT_WATCHLIST").strip() or "YT_WATCHLIST"
WATCHLIST_THREAD_ID_OVERRIDE = _env_int("NIXE_YT_WUWA_WATCHLIST_THREAD_ID", 1453571893062926428)
WATCHLIST_THREAD_SCAN_LIMIT = _env_int("NIXE_YT_WUWA_WATCHLIST_THREAD_SCAN_LIMIT", 200)

# Watchlist thread store message (keeps thread clean)
WATCHLIST_STORE_MARKER = "[yt-wuwa-watchlist]"
WATCHLIST_STORE_ATTACHMENT_NAME = "youtube_wuwa_watchlist.json"
WATCHLIST_CLEAN_THREAD = os.getenv("NIXE_YT_WUWA_WATCHLIST_CLEAN_THREAD", "1").strip() == "1"
WATCHLIST_STORE_MAX_HISTORY_SCAN = _env_int("NIXE_YT_WUWA_WATCHLIST_STORE_MAX_HISTORY_SCAN", 50)



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

def _strip_youtube_suffix(s: str) -> str:
    s = (s or "").strip()
    # Most channel pages set <title> "<Channel Name> - YouTube"
    if s.lower().endswith(" - youtube"):
        s = s[:-10].rstrip()
    return s

_OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]+)"', re.IGNORECASE)
_META_TITLE_RE = re.compile(r'<meta\s+name="title"\s+content="([^"]+)"', re.IGNORECASE)
_TITLE_TAG_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_CHANNEL_ID_RE = re.compile(r'"channelId"\s*:\s*"(UC[0-9A-Za-z_-]{20,})"')
_UC_ID_LIKE_RE = re.compile(r"^UC[0-9A-Za-z_-]{20,}$")

def _extract_channel_title_from_html(html: str) -> Optional[str]:
    if not html:
        return None
    m = _OG_TITLE_RE.search(html) or _META_TITLE_RE.search(html)
    if m:
        return _strip_youtube_suffix(_html.unescape(m.group(1)))
    m2 = _TITLE_TAG_RE.search(html)
    if m2:
        txt = re.sub(r"\s+", " ", _html.unescape(m2.group(1))).strip()
        return _strip_youtube_suffix(txt)

    # Fallback: try to extract channel title from ytInitialData channelMetadataRenderer
    try:
        blob = _extract_yt_var_json(html, "ytInitialData") or _extract_json_blob(html, _YTID_RE)
        if isinstance(blob, dict):
            # Walk a few common paths
            md = (blob.get("metadata") or {}).get("channelMetadataRenderer") if isinstance(blob.get("metadata"), dict) else None
            if isinstance(md, dict):
                t = (md.get("title") or "").strip()
                t = _strip_youtube_suffix(t)
                if t:
                    return t
    except Exception:
        pass
    return None

def _extract_channel_id_from_html(html: str) -> Optional[str]:
    if not html:
        return None
    m = _CHANNEL_ID_RE.search(html)
    return m.group(1) if m else None

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
_YTID_RE = re.compile(r"ytInitialData\s*=\s*(\{.*?\})\s*;\s*</script>", re.S)
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

def _yt_live_info(player: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], bool, Optional[datetime], Optional[str]]:
    """
    Returns (video_id, title, is_live_now, start_ts_utc, channel_name).

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

    # Channel display name (prefer microformat ownerChannelName; fallback to videoDetails.author)
    channel_name = None
    try:
        if isinstance(micro, dict):
            channel_name = micro.get("ownerChannelName") or None
        if not channel_name and isinstance(vd, dict):
            channel_name = vd.get("author") or None
        if isinstance(channel_name, str):
            channel_name = channel_name.strip() or None
    except Exception:
        channel_name = None

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

    return vid, title, is_live_now, start_ts, channel_name
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
        self._loop_started = False
    @commands.Cog.listener()
    async def on_ready(self):
        if getattr(self, '_loop_started', False):
            return
        self._loop_started = True
        try:
            if not self.loop.is_running():
                self.loop.start()
        except Exception:
            pass

    async def cog_load(self) -> None:
        # Register persistent watchlist pager buttons (Render-safe).
        try:
            if _YTWatchlistPager is not None:
                # total_pages is irrelevant for dispatch; only custom_id mapping matters.
                self.bot.add_view(_YTWatchlistPager(self, page=1, total_pages=2))
        except Exception:
            pass

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

        # Canonicalize/dedupe targets for stable runtime & display.
        tlist_raw = cfg.get("targets") or []
        try:
            merged, _, _ = self._merge_targets([], tlist_raw)
            tlist: List[Dict[str, str]] = [t for t in merged if isinstance(t, dict)]
            cfg["targets"] = tlist
        except Exception:
            tlist = [t for t in tlist_raw if isinstance(t, dict)]

        out: List[Target] = []
        for t in tlist:
            if not isinstance(t, dict):
                continue

            name = str(t.get("name") or t.get("channel_name") or "").strip()
            handle = str(t.get("handle") or "").strip()
            channel_id = str(t.get("channel_id") or "").strip()
            url = str(t.get("url") or "").strip()

            if name.startswith("@"):
                name = name[1:].strip()

            if not name:
                # Leave blank: name must be the channel display name (resolved later).
                name = ""

            if (not name) and (not handle) and (not channel_id) and (not url):
                continue

            url = self._canonicalize_youtube_channel_url(url) if url else ""
            query = str(t.get("query") or handle or channel_id or name)

            out.append(Target(
                name=name,
                query=query,
                handle=handle,
                channel_id=channel_id,
                url=url,
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


    @staticmethod
    def _canonicalize_youtube_channel_url(url: str) -> str:
        """Return canonical channel URL (no query/fragment, no /videos suffix).
        Best-effort; never raises.
        """
        u = (url or "").strip()
        if not u:
            return ""

        # Normalize scheme
        if u.startswith("//"):
            u = "https:" + u
        if not re.match(r"^https?://", u, re.IGNORECASE):
            u = "https://" + u.lstrip("/")

        try:
            pu = urlparse(u)
            netloc = (pu.netloc or "").lower()
            if netloc in ("youtube.com", "www.youtube.com", "m.youtube.com"):
                netloc = "www.youtube.com"

            path = pu.path or ""
            # strip trailing slashes
            path = re.sub(r"/+$", "", path)

            # common suffix pages
            for suf in ("/videos", "/featured", "/streams", "/live", "/community", "/about", "/playlists", "/shorts"):
                if path.lower().endswith(suf):
                    path = path[: -len(suf)]
                    path = re.sub(r"/+$", "", path)
                    break

            # keep only the primary identifier segment
            m = re.match(r"^/(@[0-9A-Za-z_\.\-]+)(?:/.*)?$", path, re.IGNORECASE)
            if m:
                path = "/" + m.group(1)

            m = re.match(r"^/channel/(UC[0-9A-Za-z_\-]+)(?:/.*)?$", path, re.IGNORECASE)
            if m:
                path = "/channel/" + m.group(1)

            m = re.match(r"^/(c|user)/([^/]+)(?:/.*)?$", path, re.IGNORECASE)
            if m:
                path = f"/{m.group(1)}/{m.group(2)}"

            return urlunparse(("https", netloc, path, "", "", ""))
        except Exception:
            return u.split("?")[0].split("#")[0].rstrip("/")

    @classmethod
    def _target_dedupe_key(cls, t: Dict[str, str]) -> str:
        """Compute stable dedupe key for watchlist targets."""
        cid = (t.get("channel_id") or "").strip()
        if cid:
            return f"cid:{cid}"

        h = (t.get("handle") or "").strip()
        if h.startswith("@"):
            h = h[1:]
        h = h.strip().lower()
        if h:
            return f"h:{h}"

        u = cls._canonicalize_youtube_channel_url(t.get("url") or "").lower()
        if u:
            return f"u:{u}"

        nm = (t.get("name") or t.get("channel_name") or "").strip()
        if nm.startswith("@"):
            nm = nm[1:]
        nm = unicodedata.normalize("NFKC", nm).casefold()
        return f"n:{nm}"

    @classmethod
    def _token_to_target(cls, token: str) -> Optional[Dict[str, str]]:
        """Convert a token into a watchlist target dict compatible with youtube_wuwa_watchlist.json.
        Notes:
        - Never returns a name that begins with '@' (for cleaner embeds).
        - Canonicalizes YouTube channel URLs to reduce duplicates (e.g., /videos, /featured).
        """
        if not token:
            return None
        t = token.strip()
        if not t:
            return None

        handle = ""
        url = ""
        channel_id = ""

        # handle token
        if t.startswith("@"):
            handle = t
            url = f"https://www.youtube.com/{handle}"
        elif "youtube.com" in t.lower():
            raw = t.split("?")[0].split("#")[0].rstrip("/")
            low = raw.lower()

            # channel id
            m = re.search(r"/channel/(UC[0-9A-Za-z_\-]+)", raw, re.IGNORECASE)
            if m:
                channel_id = m.group(1)

            # handle url
            m = re.search(r"/(@[0-9A-Za-z_\.\-]+)(?:/.*)?$", raw, re.IGNORECASE)
            if m:
                handle = m.group(1)

            url = cls._canonicalize_youtube_channel_url(raw)

            # accept only likely channel URLs
            if not ("/channel/" in low or "/@" in low or "/c/" in low or "/user/" in low):
                return None
        elif "youtu.be" in t.lower():
            # likely a video URL; skip (we only track channels)
            return None
        else:
            return None

        # Prefer canonical URL if we can derive it from handle/channel_id
        if handle:
            url = f"https://www.youtube.com/{handle}"
        elif channel_id:
            url = f"https://www.youtube.com/channel/{channel_id}"
        else:
            url = cls._canonicalize_youtube_channel_url(url)

        # Name: MUST be the channel display name (resolved later). Do not store handle/channel_id as name.
        name = ""

        # Query drives lookups/dedupe; prefer @handle, else UC channel id.
        query = handle or channel_id or ""
        if not query:
            # As a last resort keep something stable (canonical URL) so the target isn't dropped.
            query = url

        return {
            "name": name,
            "query": query,
            "handle": handle,
            "channel_id": channel_id,
            "url": url,
        }

    def _merge_targets(self, existing: List[Any], new_targets: List[Any]) -> Tuple[List[Any], int, List[Dict[str, str]]]:
        """Merge target dicts into existing targets list (skip dupes + upgrade fields).

        Returns:
          merged_list, added_count, added_items
        """

        def as_dict(x: Any) -> Optional[Dict[str, str]]:
            if isinstance(x, dict):
                return {k: str(v) for k, v in x.items() if v is not None}
            if isinstance(x, str):
                t = self._token_to_target(x)
                return t
            return None

        merged: List[Any] = list(existing or [])
        added = 0
        added_items: List[Dict[str, str]] = []

        # Build index for fast dedupe
        index: Dict[str, Dict[str, str]] = {}
        for it in merged:
            d = as_dict(it)
            if not d:
                continue
            # normalize url for stable keys
            if d.get("url"):
                d["url"] = self._canonicalize_youtube_channel_url(d.get("url") or "")
            key = self._target_dedupe_key(d)
            index[key] = d

        def is_better_name(cur: str, new: str) -> bool:
            cur = (cur or "").strip()
            new = (new or "").strip()
            if not new:
                return False
            if not cur:
                return True
            # If current looks like a handle (or channel_id), prefer real name
            if cur.startswith("@"):
                return True
            if _UC_ID_LIKE_RE.match(cur):
                return True
            return False

        for raw in new_targets or []:
            d = as_dict(raw)
            if not d:
                continue

            # Canonicalize
            if d.get("url"):
                d["url"] = self._canonicalize_youtube_channel_url(d.get("url") or "")
            if d.get("handle") and d["handle"].startswith("@@"):
                d["handle"] = "@" + d["handle"].lstrip("@")

            key = self._target_dedupe_key(d)
            exist = index.get(key)

            # Also attempt cross-key match: cid/handle/url variants
            if not exist:
                cid = (d.get("channel_id") or "").strip()
                if cid:
                    exist = index.get(f"cid:{cid}")
                if not exist:
                    h = (d.get("handle") or "").strip()
                    if h.startswith("@"):
                        h = h[1:]
                    if h:
                        exist = index.get(f"h:{h.lower()}")
                if not exist:
                    u = (d.get("url") or "").strip().lower()
                    if u:
                        exist = index.get(f"u:{u}")

            if exist:
                # upgrade missing fields on the existing dict
                if is_better_name(exist.get("name", ""), d.get("name", "")):
                    exist["name"] = d.get("name", "")
                for fld in ("query", "handle", "channel_id", "url"):
                    if not (exist.get(fld) or "").strip() and (d.get(fld) or "").strip():
                        exist[fld] = d.get(fld) or ""
                # ensure key index updated (in case url/handle became available)
                index[self._target_dedupe_key(exist)] = exist
                continue

            # brand new entry
            merged.append(d)
            index[key] = d
            added_items.append(d)
            added += 1

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
                allowed_mentions=allowed_mentions,
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
                if not author:
                    return None
                # Reject handles or raw channel IDs; caller will fallback to other sources.
                if author.startswith(("@", "＠")):
                    return None
                if _UC_ID_LIKE_RE.match(author):
                    return None
                return author
        except Exception:
            return None


    
    async def _try_fetch_channel_name_from_channel_page(self, url: str) -> Optional[str]:
        """Best-effort display name resolution from a channel page HTML.

        This is used as a fallback when oEmbed is blocked/unavailable.
        """
        try:
            html = await self._http_get_text(url)
            nm = _extract_channel_title_from_html(html or "")
            nm = (nm or "").strip()
            if not nm:
                return None
            # Avoid returning obvious handles or raw channel IDs.
            if nm.startswith(("@", "＠")):
                return None
            if _UC_ID_LIKE_RE.match(nm):
                return None
            return nm
        except Exception:
            return None


    def _format_watchlist_entry(self, t: Dict[str, str]) -> str:
        # Display name: prefer proper channel name; never show '@handle' as the name.
        name = (t.get("name") or t.get("channel_name") or "").strip()
        handle = (t.get("handle") or "").strip()
        cid = (t.get("channel_id") or "").strip()
        url = (t.get("url") or "").strip()

        if name.startswith("@"):
            name = name[1:].strip()

        if not name:
            if handle:
                name = handle.lstrip("@").strip()
            elif cid:
                name = cid
            else:
                name = "unknown"

        # Link: prefer canonical URL; fallback to handle/channel_id.
        link = self._canonicalize_youtube_channel_url(url) if url else ""
        if not link:
            if handle:
                link = f"https://www.youtube.com/{handle}"
            elif cid:
                link = f"https://www.youtube.com/channel/{cid}"

        return f"{name} — {link}" if link else name



    def _build_watchlist_embed(self, targets: List[Dict[str, str]], page: int = 1) -> discord.Embed:
        # Canonicalize + dedupe + sort for stable display (and to avoid dupes from URL variants).
        try:
            merged, _, _ = self._merge_targets([], targets or [])
            targets2 = [t for t in merged if isinstance(t, dict)]
        except Exception:
            targets2 = list(targets or [])

        def sort_key(d: Dict[str, str]) -> str:
            nm = (d.get("name") or d.get("channel_name") or d.get("handle") or d.get("channel_id") or "").strip()
            if nm.startswith("@"):
                nm = nm[1:]
            nm = unicodedata.normalize("NFKC", nm).casefold()
            return nm

        targets2.sort(key=sort_key)

        page_size = 60
        total = len(targets2)
        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        page = max(1, min(int(page or 1), total_pages))

        start = (page - 1) * page_size
        end = start + page_size
        page_items = targets2[start:end]

        lines: List[str] = []
        for t in page_items:
            lines.append(self._format_watchlist_entry(t))

        desc = "\n".join(lines) if lines else "(empty)"

        emb = discord.Embed(
            title="YouTube WuWa Watchlist",
            description=desc,
        )

        # Show config (must match the JSON attachment).
        try:
            enabled = os.getenv("NIXE_YT_WUWA_ANNOUNCE_ENABLE", "0").strip() == "1"
            poll_s = _env_int("NIXE_YT_WUWA_ANNOUNCE_POLL_SECONDS", POLL_SECONDS)
            conc = _env_int("NIXE_YT_WUWA_ANNOUNCE_CONCURRENCY", CONCURRENCY)
            ch_id = _env_int("NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID", ANNOUNCE_CHANNEL_ID)
            native = os.getenv("NIXE_YT_WUWA_ANNOUNCE_NATIVE_EMBED", "1").strip() == "1"
            emb.add_field(
                name="Config",
                value=(
                    f"Enabled: {enabled}\n"
                    f"Poll: {poll_s}s\n"
                    f"Concurrency: {conc}\n"
                    f"Channel: {ch_id}\n"
                    f"Native Embed: {native}"
                ),
                inline=False,
            )
        except Exception:
            pass

        emb.set_footer(text=f"Page {page}/{total_pages} • {total} channel(s) • Auto-synced from thread")
        return emb


    def _build_watchlist_view_for_targets(self, targets: List[Dict[str, str]], page: int = 1) -> Optional[discord.ui.View]:
        """Return a pager view if watchlist exceeds one page; otherwise None."""
        if _YTWatchlistPager is None:
            return None
        try:
            merged, _, _ = self._merge_targets([], targets or [])
            total = len([t for t in merged if isinstance(t, dict)])
        except Exception:
            total = len(targets or [])

        page_size = 60
        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        if total_pages <= 1:
            return None
        return _YTWatchlistPager(self, page=page, total_pages=total_pages)


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
            targets = self.watch.get("targets") or []
            try:
                merged, _, _ = self._merge_targets([], targets or [])
                targets = [t for t in merged if isinstance(t, dict)]
            except Exception:
                targets = list(targets or [])
            emb = self._build_watchlist_embed(targets, page=1)
            view = self._build_watchlist_view_for_targets(targets, page=1)
            cfg = _read_json_any(WATCHLIST_PATH) or {}

            try:
                cfg_out = dict(cfg)
            except Exception:
                cfg_out = {}
            cfg_out["targets"] = list(targets)

            try:
                cfg_out["enabled"] = os.getenv("NIXE_YT_WUWA_ANNOUNCE_ENABLE", "0").strip() == "1"
                cfg_out["poll_seconds"] = _env_int("NIXE_YT_WUWA_ANNOUNCE_POLL_SECONDS", POLL_SECONDS)
                cfg_out["concurrency"] = _env_int("NIXE_YT_WUWA_ANNOUNCE_CONCURRENCY", CONCURRENCY)
                cfg_out["announce_channel_id"] = _env_int("NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID", ANNOUNCE_CHANNEL_ID)
                cfg_out["announce_native_embed"] = os.getenv("NIXE_YT_WUWA_ANNOUNCE_NATIVE_EMBED", "1").strip() == "1"
            except Exception:
                pass

            payload = self._build_watchlist_attachment_bytes(cfg_out)
            fp = io.BytesIO(payload)
            file = discord.File(fp=fp, filename=WATCHLIST_STORE_ATTACHMENT_NAME)
            m = await th.send(WATCHLIST_STORE_MARKER, embed=emb, view=view, file=file, allowed_mentions=discord.AllowedMentions.none())
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
            emb = self._build_watchlist_embed(targets, page=1)
            view = self._build_watchlist_view_for_targets(targets, page=1)

            # Persist canonical watchlist into the store message attachment so it survives restarts / ephemeral disk.
            try:
                cfg_out = dict(cfg)
            except Exception:
                cfg_out = {}
            cfg_out["targets"] = list(targets)

            # Force cfg values to follow current runtime env (so thread JSON + embed stay consistent).
            try:
                cfg_out["enabled"] = os.getenv("NIXE_YT_WUWA_ANNOUNCE_ENABLE", "0").strip() == "1"
                cfg_out["poll_seconds"] = _env_int("NIXE_YT_WUWA_ANNOUNCE_POLL_SECONDS", POLL_SECONDS)
                cfg_out["concurrency"] = _env_int("NIXE_YT_WUWA_ANNOUNCE_CONCURRENCY", CONCURRENCY)
                cfg_out["announce_channel_id"] = _env_int("NIXE_YT_WUWA_ANNOUNCE_CHANNEL_ID", ANNOUNCE_CHANNEL_ID)
                cfg_out["announce_native_embed"] = os.getenv("NIXE_YT_WUWA_ANNOUNCE_NATIVE_EMBED", "1").strip() == "1"
            except Exception:
                pass

            payload = self._build_watchlist_attachment_bytes(cfg_out)
            fp = io.BytesIO(payload)
            file = discord.File(fp=fp, filename=WATCHLIST_STORE_ATTACHMENT_NAME)

            try:
                # discord.py 2.x supports replacing attachments via Message.edit(attachments=[...])
                await store.edit(content=WATCHLIST_STORE_MARKER, embed=emb, view=view, attachments=[file], allowed_mentions=discord.AllowedMentions.none())
            except TypeError:
                # Fallback: cannot edit attachments; create a new store message (do NOT delete the old one).
                m2 = await th.send(WATCHLIST_STORE_MARKER, embed=emb, view=view, file=file, allowed_mentions=discord.AllowedMentions.none())
                self.state["watchlist_store_mid"] = m2.id
                _write_json_best_effort(STATE_PATH, self.state)

                # Archive the previous store message in-place (keep it for safety; never delete thread history).
                try:
                    await store.edit(
                        content=f"{WATCHLIST_STORE_MARKER} (archived; superseded by {m2.id})",
                        embed=None,
                        allowed_mentions=allowed_mentions,
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
                url_norm = _norm(it.get("url") or "")
                handle_norm = (it.get("handle") or "").strip().lower()
                cid = (it.get("channel_id") or "").strip()
                if not url_norm and handle_norm:
                    url_norm = _norm(f"https://www.youtube.com/{handle_norm}")
                if not url_norm and cid:
                    url_norm = _norm(f"https://www.youtube.com/channel/{cid}")

                if not url_norm:
                    continue

                nm = await self._try_fetch_channel_name_oembed(url_norm)
                if not nm:
                    nm = await self._try_fetch_channel_name_from_channel_page(url_norm)
                if not nm:
                    continue

                for t in targets:
                    if not isinstance(t, dict):
                        continue

                    t_url_norm = _norm(t.get("url") or "")
                    t_handle_norm = (t.get("handle") or "").strip().lower()
                    t_cid = (t.get("channel_id") or "").strip()

                    if (url_norm and t_url_norm and url_norm == t_url_norm) or (handle_norm and t_handle_norm and handle_norm == t_handle_norm) or (cid and t_cid and cid == t_cid):
                        cur = (t.get("name") or "").strip()
                        cur_norm = _norm(cur)
                        url_match_norm = _norm(t.get("url") or "")

                        need = (
                            (not cur)
                            or (handle_norm and cur_norm == handle_norm)
                            or (cur.startswith(("@", "＠")) and handle_norm and cur_norm == handle_norm)
                            or (_UC_ID_LIKE_RE.match(cur) and t_cid and cur == t_cid)
                            or (cur_norm.startswith("http"))
                            or (url_match_norm and cur_norm == url_match_norm)
                        )
                        if not need:
                            continue

                        t["name"] = nm
                        qcur = (t.get("query") or "").strip()
                        qnorm = _norm(qcur)
                        # Keep query stable: prefer @handle when available; never replace a handle/channel_id query with display name.
                        preferred = (t.get("handle") or "").strip()
                        if preferred and not preferred.startswith("@"):
                            preferred = "@" + preferred
                        if not preferred:
                            preferred = (t.get("channel_id") or "").strip() or nm

                        # Only rewrite query if it is empty / generic (URL-like/UC-like) or already equals the display name.
                        if (not qcur) or (qnorm.startswith("http")) or (_UC_ID_LIKE_RE.match(qcur)) or (_norm(nm) and qnorm == _norm(nm)):
                            t["query"] = preferred
                        changed = True

            if changed:
                cfg["targets"] = list(targets)
                _write_json_best_effort(WATCHLIST_PATH, cfg)
                self._reload_watchlist()
        except Exception:
            pass



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
        # Best-effort: resolve channel name for new additions so logs + embed use the YouTube
        # display name (not @handle/UC.../URL tokens). Must never block ingest.
        for t in added_items:
            if not isinstance(t, dict):
                continue
            cur = (t.get("name") or t.get("channel_name") or "").strip()
            handle = (t.get("handle") or "").strip()
            handle_norm = handle.strip().lower()
            cid = (t.get("channel_id") or "").strip()
            url = (t.get("url") or "").strip()

            cur_norm = cur.strip().rstrip("/").lower()
            url_norm = url.strip().rstrip("/").lower()

            need = (
                (not cur)
                or (handle_norm and cur_norm == handle_norm)
                or (cur.startswith(("@", "＠")) and handle_norm and cur_norm == handle_norm)
                or (_UC_ID_LIKE_RE.match(cur) and cid and cur == cid)
                or (cur_norm.startswith("http"))
                or (url_norm and cur_norm == url_norm)
            )
            if not need:
                continue

            if not url and handle.strip().startswith("@"):
                url = f"https://www.youtube.com/{handle.strip()}"
            if not url and cid:
                url = f"https://www.youtube.com/channel/{cid}"
            if not url:
                continue

            try:
                nm = await self._try_fetch_channel_name_oembed(url)
                if not nm:
                    nm = await self._try_fetch_channel_name_from_channel_page(url)
                if nm:
                    t["name"] = nm
                    qcur = (t.get("query") or "").strip()
                    qnorm = qcur.strip().rstrip("/").lower()
                    # Keep query stable unless it is also generic.
                    # Keep query stable: prefer @handle when available; never replace a handle/channel_id query with display name.
                    preferred = (t.get("handle") or "").strip()
                    if preferred and not preferred.startswith("@"):
                        preferred = "@" + preferred
                    if not preferred:
                        preferred = (t.get("channel_id") or "").strip() or nm

                    nm_norm = (nm or "").strip().rstrip("/").lower()
                    # Only rewrite query if it is empty / generic (URL-like/UC-like) or already equals the display name.
                    if (not qcur) or (qnorm.startswith("http")) or (_UC_ID_LIKE_RE.match(qcur)) or (nm_norm and qnorm == nm_norm):
                        t["query"] = preferred
            except Exception:
                pass

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

            ch = message.channel

            # Robust watchlist thread detection:
            # - Prefer exact thread id when known
            # - Fallback to thread name match (avoids stale hardcoded IDs breaking auto-delete)
            if not isinstance(ch, discord.Thread):
                return

            want_name = (WATCHLIST_THREAD_NAME or "").strip().lower()
            ch_name = (getattr(ch, "name", "") or "").strip().lower()

            if self.watchlist_thread_id and int(getattr(ch, "id", 0) or 0) == int(self.watchlist_thread_id):
                pass
            elif want_name and ch_name == want_name:
                # Bind on first sight so subsequent checks use the correct id.
                self.watchlist_thread_id = int(getattr(ch, "id", 0) or 0)
                self.watchlist_thread = ch
            else:
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
        if t.channel_id or t.url:
            return t

        res = self.state.get("resolved", {})
        cached = (res.get(t.handle) if t.handle else None) or (res.get(t.url) if t.url else None) or res.get(t.channel_id) or res.get(t.query) or res.get(t.name)
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

    async def _check_live(self, t: Target) -> Optional[Tuple[Target, str, str, Optional[datetime], str]]:
        """
        Returns (target, video_id, title, start_ts_utc, creator_name) if live now and matches whitelist.
        """
        t = await self._resolve_channel(t)
        base = t.base_url()
        if not base:
            return None
        # Best-effort: resolve the real channel display name for this target so announces use the
        # YouTube channel display name (not the @handle token). Must never block announcing.
        try:
            need_name = (t.name or "").strip().startswith(("@", "＠")) or (
                (t.handle or "").strip() and (t.name or "").strip() == (t.handle or "").strip()
            )
            if need_name:
                res = self.state.setdefault("resolved", {})
                cached = None
                if t.handle:
                    cached = res.get(t.handle)
                if not cached and t.query:
                    cached = res.get(t.query)
                if not cached:
                    cached = res.get(t.name)
                nm = None
                if isinstance(cached, dict):
                    nm = (cached.get("title") or "").strip() or None
                if not nm:
                    nm = await self._try_fetch_channel_name_oembed(base)
                if nm:
                    t.name = nm
                    if t.handle:
                        res[t.handle] = {"channel_id": t.channel_id, "title": t.name, "url": (t.url or base)}
                    if t.query:
                        res.setdefault(t.query, {"channel_id": t.channel_id, "title": t.name, "url": (t.url or base)})
                    _write_json_best_effort(STATE_PATH, self.state)
        except Exception:
            pass
        live_url = base.rstrip("/") + "/live"
        html = await self._http_get_text(live_url)
        if not html:
            return None

        # Fast path: parse ytInitialPlayerResponse (scrape)
        player = _extract_yt_var_json(html, 'ytInitialPlayerResponse') or _extract_json_blob(html, _YTIPR_RE)
        if player:
            vid, title, is_live_now, start_ts, ch_name = _yt_live_info(player)
            # Prefer channel display name from the player response (no extra HTTP, works even if oEmbed is blocked).
            try:
                nm0 = (ch_name or "").strip()
                if nm0 and (not nm0.startswith(("@", "＠"))) and (not _UC_ID_LIKE_RE.match(nm0)):
                    t.name = nm0
                    # cache resolved name for stability across restarts
                    res = self.state.setdefault("resolved", {})
                    for kk in [t.channel_id, t.url or t.base_url() or "", t.handle, t.query, t.name]:
                        if kk:
                            res.setdefault(str(kk), {"channel_id": t.channel_id, "title": t.name, "url": (t.url or t.base_url() or "")})
                    _write_json_best_effort(STATE_PATH, self.state)
            except Exception:
                pass
            if not (vid and title and is_live_now):
                return None
        else:
            # Fallback path (NO YouTube API):
            # - Extract a probable live videoId from the /live page HTML.
            # - Fetch the canonical watch page and parse ytInitialPlayerResponse there.
            vid = self._extract_video_id_fallback(html)
            if not vid:
                return None

            watch_url = f"https://www.youtube.com/watch?v={vid}"
            watch_html = await self._http_get_text(watch_url)
            if not watch_html:
                return None

            player2 = _extract_yt_var_json(watch_html, 'ytInitialPlayerResponse') or _extract_json_blob(watch_html, _YTIPR_RE)
            player = player2
            if not player2:
                return None

            vid2, title2, is_live_now2, start_ts2, ch_name2 = _yt_live_info(player2)
            if not (vid2 and title2 and is_live_now2):
                return None

            vid, title, is_live_now, start_ts = vid2, title2, is_live_now2, start_ts2

            # Prefer channel display name from the player response.
            try:
                nm0 = (ch_name2 or "").strip()
                if nm0 and (not nm0.startswith(("@", "＠"))) and (not _UC_ID_LIKE_RE.match(nm0)):
                    t.name = nm0
                    res = self.state.setdefault("resolved", {})
                    for kk in [t.channel_id, t.url or t.base_url() or "", t.handle, t.query, t.name]:
                        if kk:
                            res[str(kk)] = nm0
                    res.setdefault("url_to_name", {})[str(t.base_url() or t.url or "")] = nm0
                    _write_json_best_effort(STATE_PATH, self.state)
            except Exception:
                pass



        # Ensure creator display name is the YouTube channel name (not @handle).
        # Resolve using oEmbed on the live video watch URL (most reliable). Must never block announcing.
        try:
            need_name2 = False
            nm_current = (t.name or "").strip()
            if (not nm_current) or nm_current.startswith(("@", "＠")) or _UC_ID_LIKE_RE.match(nm_current):
                need_name2 = True
            if (t.handle or "").strip() and nm_current and nm_current == (t.handle or "").strip():
                need_name2 = True

            if need_name2:
                res = self.state.setdefault("resolved", {})

                def _pick_cached(keys):
                    for kk in keys:
                        if not kk:
                            continue
                        cc = res.get(str(kk))
                        if isinstance(cc, dict):
                            cand = (cc.get("title") or "").strip()
                            if cand and (not cand.startswith(("@", "＠"))) and (not _UC_ID_LIKE_RE.match(cand)):
                                return cand
                    return None

                base_url = (t.url or t.base_url() or "").strip()
                nm = _pick_cached([t.channel_id, base_url, t.handle, t.query, t.name])

                # Most reliable: oEmbed on the exact watch URL for the video we are about to announce.
                if not nm and vid:
                    watch_url = f"https://www.youtube.com/watch?v={vid}"
                    try:
                        nm = await asyncio.wait_for(self._try_fetch_channel_name_oembed(watch_url), timeout=3.0)
                    except Exception:
                        nm = None

                # Fallback: parse the channel page title ("<Channel> - YouTube").
                if not nm and base_url:
                    try:
                        nm = await asyncio.wait_for(self._try_fetch_channel_name_from_channel_page(base_url), timeout=4.0)
                    except Exception:
                        nm = None

                if nm:
                    t.name = nm
                    # Cache under stable identifiers so we avoid repeated fetches.
                    try:
                        for kk in {t.channel_id, base_url, t.handle, t.query}:
                            if kk:
                                res[str(kk)] = {"channel_id": t.channel_id, "title": t.name, "url": base_url}
                        _write_json_best_effort(STATE_PATH, self.state)
                    except Exception:
                        pass

        except Exception:
            pass

        if not (self.title_rx.search(title) or self.title_rx.search(_normalize_title(title))):
            return None
        # Final creator display name: must be YouTube channel display name (not UC id / handle).
        creator_name = (t.name or "").strip()
        # If still looks like UC id or @handle, fall back to the best channel name we already parsed.
        try:
            if (not creator_name) or creator_name.startswith(("@", "＠")) or _UC_ID_LIKE_RE.match(creator_name):
                # Try from player response first (no extra HTTP).
                if 'player' in locals() and isinstance(player, dict):
                    try:
                        _, _, _, _, ch_name2 = _yt_live_info(player)
                        nm2 = (ch_name2 or "").strip()
                        if nm2 and (not nm2.startswith(("@", "＠"))) and (not _UC_ID_LIKE_RE.match(nm2)):
                            creator_name = nm2
                    except Exception:
                        pass
                # If API snippet exists, use channelTitle.
                if (not creator_name) or creator_name.startswith(("@", "＠")) or _UC_ID_LIKE_RE.match(creator_name):
                    if 'snippet' in locals() and isinstance(snippet, dict):
                        try:
                            nm3 = (snippet.get("channelTitle") or "").strip()
                            if nm3 and (not nm3.startswith(("@", "＠"))) and (not _UC_ID_LIKE_RE.match(nm3)):
                                creator_name = nm3
                        except Exception:
                            pass
        except Exception:
            pass
        # If still invalid, try cached channel display name (survives restarts via STATE_PATH).
        try:
            cid_cache_key = (getattr(t, "channel_id", None) or getattr(t, "base_id", None) or "").strip()
            if cid_cache_key:
                ccache = self.state.get("yt_channel_name_cache", {})
                if (creator_name.startswith(("@", "＠")) or _UC_ID_LIKE_RE.match(creator_name)) and isinstance(ccache, dict):
                    cached_nm = (ccache.get(cid_cache_key) or "").strip()
                    if cached_nm and (not cached_nm.startswith(("@", "＠"))) and (not _UC_ID_LIKE_RE.match(cached_nm)):
                        creator_name = cached_nm
        except Exception:
            pass

        # Update cache when we have a good resolved name.
        try:
            cid_cache_key = (getattr(t, "channel_id", None) or getattr(t, "base_id", None) or "").strip()
            if cid_cache_key and creator_name and (not creator_name.startswith(("@", "＠"))) and (not _UC_ID_LIKE_RE.match(creator_name)):
                ccache = self.state.setdefault("yt_channel_name_cache", {})
                if isinstance(ccache, dict) and ccache.get(cid_cache_key) != creator_name:
                    ccache[cid_cache_key] = creator_name
                    _write_json_best_effort(STATE_PATH, self.state)
        except Exception:
            pass

        # Final sanitize: never allow UC ids / handles to be used as the channel display name.
        if creator_name.startswith(("@", "＠")) or _UC_ID_LIKE_RE.match(creator_name):
            creator_name = ""

        # Absolute last resort: DO NOT announce if we still don't have a valid channel display name.
        if not creator_name:
            cand_nm = (t.name or t.query or "").strip()
            if cand_nm and (not cand_nm.startswith(("@", "＠"))) and (not _UC_ID_LIKE_RE.match(cand_nm)):
                creator_name = cand_nm
        return t, vid, title, start_ts, creator_name
    def _render_template(self, creator_name: str, video_link: str) -> str:
        msg = self.template
        msg = msg.replace("{creator.name}", creator_name)
        msg = msg.replace("{video.link}", video_link)
        return msg


    async def _post(self, channel: discord.TextChannel, creator_name: str, title: str, video_id: str):
        # Use the canonical watch URL so Discord is more likely to render the native YouTube player-style embed.
        video_link = f"https://www.youtube.com/watch?v={video_id}"
        content = self._render_template(creator_name, video_link)

        role_id = NOTIFY_ROLE_ID
        if role_id:
            content = f"<@&{role_id}> {content}"
            allowed_mentions = discord.AllowedMentions(roles=True, users=False, everyone=False)
        else:
            allowed_mentions = discord.AllowedMentions.none()

        # If we are using custom embeds, prevent Discord from also unfurling the raw URL in the message content.
        # Wrapping the URL in angle brackets suppresses native embeds while keeping it clickable.
        if not ANNOUNCE_NATIVE_EMBED:
            try:
                if video_link and f"<{video_link}>" not in content:
                    content = content.replace(video_link, f"<{video_link}>")
            except Exception:
                pass

        # Native embed mode: do NOT attach a custom embed/image; let Discord unfurl the YouTube link.
        if ANNOUNCE_NATIVE_EMBED:
            try:
                await channel.send(
                    content=content,
                    allowed_mentions=allowed_mentions,
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
                allowed_mentions=allowed_mentions,
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

                timeout = CHECK_TIMEOUT_SECONDS

                return await asyncio.wait_for(self._check_live(tt), timeout=timeout)
            except Exception as e:

                if DEBUG:

                    try:

                        q = getattr(tt, "query", None) if isinstance(tt, dict) else 'unknown'

                    except Exception:

                        q = 'unknown'

                    log.info('[yt-wuwa] check timeout/err for %s: %r', q, e)

                return None


        tasks_list = [asyncio.create_task(_run_one(t)) for t in list(self.targets)]

        deadline = LOOP_DEADLINE_SECONDS

        done, pending = await asyncio.wait(tasks_list, timeout=deadline)
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
            t, vid, title, start_ts, creator_name = res

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
            if str(vid) in ann_vids or str(vid) in _INFLIGHT_VIDS or str(vid) in set(str(v) for v in ann_map.values()):
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
                if not creator_name:
                    log.warning("[yt-wuwa] cannot resolve channel display name (target=%s vid=%s). Set NIXE_YT_WUWA_YT_API_KEY (YouTube API v3) or set an explicit display name in watchlist.", getattr(t, "name", "unknown"), vid)
                    continue
                # In-flight de-dupe: prevents double-post if multiple loop instances race on the same video_id.
                vid_s = str(vid)
                try:
                    async with _ANNOUNCE_LOCK:
                        if vid_s in _INFLIGHT_VIDS:
                            continue
                        _INFLIGHT_VIDS.add(vid_s)
                except Exception:
                    pass

                try:
                    await self._post(ch, creator_name, title, vid)
                finally:
                    try:
                        async with _ANNOUNCE_LOCK:
                            _INFLIGHT_VIDS.discard(vid_s)
                    except Exception:
                        pass
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

        # Merge local watchlist with the persistent store message attachment (do not overwrite deploy watchlist).
        try:
            th0 = await self._ensure_watchlist_thread()
            if th0:
                sc0 = await self._load_watchlist_from_store_attachment(th0)
                if isinstance(sc0, dict) and sc0.get("targets"):
                    cfg_local = _read_json_any(WATCHLIST_PATH) or {}
                    local_targets = list(cfg_local.get("targets") or [])

                    # Sanitize store targets: never keep '@...' as name; prefer enrichment.
                    store_targets = []
                    for t in list(sc0.get("targets") or []):
                        if not isinstance(t, dict):
                            continue
                        nm = (t.get("name") or "").strip()
                        if nm.startswith(("@", "＠")):
                            t = dict(t)
                            t["name"] = ""
                        store_targets.append(t)

                    merged, added, added_items = self._merge_targets(local_targets, store_targets)

                    # Keep deploy config as primary; backfill missing keys from store.
                    for k in ("enabled", "title_whitelist_regex", "message_template", "max_age_minutes"):
                        if (k not in cfg_local) or (cfg_local.get(k) in (None, "")):
                            if sc0.get(k) not in (None, ""):
                                cfg_local[k] = sc0.get(k)

                    cfg_local["targets"] = merged
                    _write_json_best_effort(WATCHLIST_PATH, cfg_local)
                    self._reload_watchlist()

                    # Best-effort: resolve display names for any store-added items so embeds don't show handles as names.
                    if added_items:
                        await self._enrich_watchlist_names(added_items)

                    if added:
                        log.info("[yt-wuwa] merged %d target(s) from store attachment into local watchlist", added)
        except Exception as e:
            log.warning("[yt-wuwa] watchlist store attachment merge failed: %r", e)

        try:
            await self._bootstrap_watchlist_from_thread()
        except Exception as e:
            log.warning("[yt-wuwa] watchlist bootstrap failed: %r", e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(YouTubeWuWaLiveAnnouncer(bot))
