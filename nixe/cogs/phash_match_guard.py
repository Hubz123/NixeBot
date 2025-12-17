# nixe/cogs/phash_match_guard.py
from __future__ import annotations

import os
import re
import json
import time
import logging
from io import BytesIO
from typing import Optional, List, Set, Tuple, Dict

import discord
from discord.ext import commands

from nixe.helpers.ban_utils import emit_phish_detected

log = logging.getLogger("nixe.cogs.phash_match_guard")

try:
    from PIL import Image as _PIL_Image  # type: ignore
except Exception:  # pragma: no cover
    _PIL_Image = None

try:
    import imagehash as _imagehash  # type: ignore
except Exception:  # pragma: no cover
    _imagehash = None

HEX16 = re.compile(r"^[0-9a-f]{16}$", re.I)

PHASH_DB_MARKER = (os.getenv("PHASH_DB_MARKER", "NIXE_PHASH_DB_V1") or "NIXE_PHASH_DB_V1").strip()
PHASH_SOURCE_THREAD_ID = int(
    (os.getenv("PHASH_IMAGEPHISH_THREAD_ID")
     or os.getenv("NIXE_PHASH_SOURCE_THREAD_ID")
     or os.getenv("PHASH_SOURCE_THREAD_ID")
     or "0")
)

# Only enforce ban on WEBP <= 1MB (as requested).
WEBP_MAX_BYTES = int(os.getenv("PHISH_WEBP_MAX_BYTES", "1048576"))

# Optional: avoid rescanning duplicate URLs repeatedly
SEEN_TTL_SEC = int(os.getenv("PHASH_MATCH_SEEN_TTL_SEC", "300"))

def _looks_like_webp(b: bytes) -> bool:
    return bool(b) and b.startswith(b"RIFF") and len(b) >= 12 and b[8:12] == b"WEBP"

def _compute_phash(raw: bytes) -> Optional[str]:
    if _PIL_Image is None or _imagehash is None:
        return None
    try:
        im = _PIL_Image.open(BytesIO(raw)).convert("RGB")
        return str(_imagehash.phash(im))
    except Exception:
        return None

def _extract_db_hashes_from_content(content: str) -> List[str]:
    """Extract pHash entries from one or more fenced JSON blocks.

    Supports:
      - content that includes PHASH_DB_MARKER plus one or more ```json {..}``` blocks
      - content that includes fenced JSON blocks without the marker (thread is expected to be dedicated)
    Returns lowercased hex strings (deduped, order preserved).
    """
    if not content:
        return []
    if ("```json" not in content) and ("```" not in content):
        return []

    out: List[str] = []
    seen: Set[str] = set()

    # Collect all json code blocks (some DB threads split into multiple pinned messages)
    for mm in re.finditer(r"```json\s*(\{.*?\})\s*```", content or "", re.I | re.S):
        try:
            obj = json.loads(mm.group(1))
            arr = obj.get("phash") or obj.get("items") or obj.get("hashes") or []
            for it in arr:
                if isinstance(it, str) and HEX16.match(it):
                    h = it.lower()
                    if h not in seen:
                        out.append(h); seen.add(h)
                elif isinstance(it, dict):
                    h = it.get("hash") or it.get("phash")
                    if isinstance(h, str) and HEX16.match(h):
                        h2 = h.lower()
                        if h2 not in seen:
                            out.append(h2); seen.add(h2)
        except Exception:
            continue

    return out

def _hamm(a: str, b: str) -> int:
    return sum(1 for x, y in zip(a, b) if x != y)

class NixePhashMatchGuard(commands.Cog):
    """Ban-on-match for known-bad WEBP images based on pHash DB thread."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._db_cache: Tuple[float, List[str]] = (0.0, [])
        self._seen: Dict[str, float] = {}  # url -> last_ts
        self._cache_ttl = int(os.getenv("PHASH_DB_CACHE_TTL_SEC", "600"))
        self._max_bits = int(os.getenv("PHASH_MATCH_WEBP_MAX_BITS", os.getenv("PHASH_HAMMING_MAX", "0") or "0") or 0)

    def _seen_recent(self, url: str) -> bool:
        if not url:
            return True
        now = time.time()
        # purge occasionally
        if len(self._seen) > 2048:
            cutoff = now - SEEN_TTL_SEC
            self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        ts = self._seen.get(url, 0.0)
        if ts and (now - ts) < SEEN_TTL_SEC:
            return True
        self._seen[url] = now
        return False

    async def _resolve_source_thread(self, guild: discord.Guild) -> Optional[discord.Thread]:
        tid = int(PHASH_SOURCE_THREAD_ID or 0)
        if not tid or not guild:
            return None
        ch = self.bot.get_channel(tid) or guild.get_thread(tid)
        if isinstance(ch, discord.Thread):
            return ch
        # fetch
        try:
            fetched = await self.bot.fetch_channel(tid)
            if isinstance(fetched, discord.Thread):
                return fetched
        except Exception:
            return None
        return None

    async def _load_db_hashes(self, guild: discord.Guild) -> List[str]:
        now = time.time()
        cached_ts, cached = self._db_cache
        if cached and (now - cached_ts) < self._cache_ttl:
            return cached

        thread = await self._resolve_source_thread(guild)
        if not thread:
            self._db_cache = (now, [])
            return []

        max_items = int(os.getenv("PHASH_DB_MAX_ITEMS", "20000"))
        history_limit = int(os.getenv("PHASH_DB_HISTORY_SCAN_LIMIT", "300"))

        # Collect from ALL pinned DB messages + recent history (DB can be split across multiple posts).
        collected: List[str] = []
        seen: Set[str] = set()

        def _add(arr: List[str]):
            for h in (arr or []):
                if h and (h not in seen):
                    collected.append(h)
                    seen.add(h)
                    if len(collected) >= max_items:
                        return True
            return False

        # 1) Pins first (fast + stable)
        try:
            pins = await thread.pins()
        except Exception:
            pins = []

        for m in (pins or []):
            try:
                c = (m.content or "")
                if ("```json" not in c) and ("```" not in c):
                    continue
                if PHASH_DB_MARKER in c or os.getenv("PHASH_DB_ACCEPT_NO_MARKER", "1") == "1":
                    if _add(_extract_db_hashes_from_content(c)):
                        self._db_cache = (now, collected)
                        return collected
            except Exception:
                pass

        # 2) Recent history scan (fallback + merge)
        try:
            async for m in thread.history(limit=history_limit):
                c = (m.content or "")
                if ("```json" not in c) and ("```" not in c):
                    continue
                if PHASH_DB_MARKER in c or os.getenv("PHASH_DB_ACCEPT_NO_MARKER", "1") == "1":
                    if _add(_extract_db_hashes_from_content(c)):
                        self._db_cache = (now, collected)
                        return collected
        except Exception:
            pass

        self._db_cache = (now, collected)
        return collected


    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        try:
            if not message or not message.guild:
                return
            if message.author and getattr(message.author, "bot", False):
                return
            if not message.attachments:
                return

            # Only process WEBP <= 1MB (including fake extension: we sniff from bytes after read).
            for att in message.attachments:
                url = getattr(att, "url", "") or ""
                if not url or self._seen_recent(url):
                    continue

                try:
                    size = int(getattr(att, "size", 0) or 0)
                except Exception:
                    size = 0
                if size and size > WEBP_MAX_BYTES:
                    continue

                # read bytes (<= 1MB)
                try:
                    raw = await att.read()
                except Exception:
                    continue
                if not raw:
                    continue
                if not _looks_like_webp(raw):
                    continue

                h = _compute_phash(raw)
                if not (h and HEX16.match(h)):
                    continue

                db_hashes = await self._load_db_hashes(message.guild)
                if not db_hashes:
                    continue

                matched = False
                best_bits = 999
                if self._max_bits <= 0:
                    matched = (h.lower() in db_hashes)
                    best_bits = 0 if matched else 999
                else:
                    for dh in db_hashes:
                        if len(dh) == len(h):
                            bits = _hamm(h.lower(), dh.lower())
                            if bits < best_bits:
                                best_bits = bits
                            if bits <= self._max_bits:
                                matched = True
                                break

                if not matched:
                    continue

                # Emit internal event -> phish_ban_embed will delete+ban (pHash-only).
                details = {
                    "score": 1.0,
                    "provider": "phash",
                    "kind": "phash",
                    "reason": f"pHash match (bits={0 if self._max_bits<=0 else best_bits}) hash={h}",
                }
                ev_urls = [getattr(a, "url", None) for a in (message.attachments or [])]
                ev_urls = [x for x in ev_urls if x]
                emit_phish_detected(self.bot, message, details, evidence_urls=ev_urls)
                return

        except Exception as e:
            log.debug("[phash-match] err: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(NixePhashMatchGuard(bot))
