# -*- coding: utf-8 -*-
"""
a17_lpg_cache_persistence_overlay
--------------------------------
Thread-backed *permanent* memory for LPG (Lucky Pull Guard).

This cog treats a single Discord thread as the source of truth for "LUCKY" memory.
- On boot: rebuilds in-RAM cache by scanning that thread.
- On message delete in that thread: immediately unlearn (remove) from in-RAM cache.
- On Render Free: uses conservative RAM limits and conservative boot/backfill limits.
- On minipc: allows unbounded RAM usage (full RAM) and enables weekly maintenance
  (footer backfill), so restarts stay fast and memory remains consistent.

Important: The thread is hardcoded as requested.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from typing import Dict, Optional, Tuple

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

# Hardcoded permanent-memory thread (per user requirement)
MEMORY_THREAD_ID = 1435924665615908965

# Footer format (parseable):
#   lpgmem sha1=<40hex> ahash=<16hex>
_FOOTER_RE = re.compile(r"sha1=([0-9a-f]{40})\s+ahash=([0-9a-f]{16})", re.I)


def _is_render_free() -> bool:
    # Render provides one or more of these env vars; do not rely on exact naming.
    for k in ("RENDER", "RENDER_INSTANCE_ID", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL"):
        if os.getenv(k):
            return True
    return False


def _is_minipc() -> bool:
    prof = (os.getenv("NIXE_RUNTIME_PROFILE") or os.getenv("RUNTIME_PROFILE") or "").strip().lower()
    return prof == "minipc"


def _env_int(k: str, default: int) -> int:
    try:
        return int(os.getenv(k, str(default)))
    except Exception:
        return default


def _env_bool(k: str, default: bool) -> bool:
    v = str(os.getenv(k, str(int(default))) or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _extract_fields_from_embed(emb: discord.Embed) -> Tuple[float, str, str, str]:
    """Return (score, provider, reason, phash_str) from the embed fields when present."""
    score = 0.0
    provider = "-"
    reason = "-"
    phash = "-"
    try:
        for f in getattr(emb, "fields", []) or []:
            name = (getattr(f, "name", "") or "").strip().lower()
            val = (getattr(f, "value", "") or "").strip()
            if name == "score":
                try:
                    score = float(val)
                except Exception:
                    score = 0.0
            elif name == "provider":
                provider = val or "-"
            elif name == "reason":
                reason = val or "-"
            elif name == "phash":
                phash = val or "-"
    except Exception:
        pass
    return score, provider, reason, phash


class LPGCachePersistence(commands.Cog):
    """Rebuild and maintain the in-RAM LPG cache from the permanent thread."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.render = _is_render_free()
        self.minipc = _is_minipc()

        # RAM cache sizing:
        # - Render Free: bounded (default 5000)
        # - minipc: unbounded by default (0 means unlimited), user will maintain weekly
        self.cache_max_entries = _env_int("LPG_CACHE_MAX_ENTRIES", 5000 if self.render else 0)

        # Boot scan limit (messages):
        # - Render Free: limit scan to avoid spikes
        # - minipc: scan full (0 => unlimited / None)
        self.boot_scan_limit = _env_int("LPG_CACHE_BOOT_SCAN_LIMIT", 3000 if self.render else 0)

        # Backfill (download attachment for messages missing footer):
        # - Render Free: small
        # - minipc: larger (weekly maintenance will finish it)
        self.backfill_per_boot = _env_int("LPG_CACHE_BACKFILL_PER_BOOT", 15 if self.render else 200)

        # Optional: write footer back onto messages missing it (minipc maintenance use-case).
        self.backfill_write_footer = _env_bool("LPG_CACHE_BACKFILL_WRITE_FOOTER", False if self.render else True)

        # Weekly maintenance only on minipc (default ON there; OFF on render)
        self.weekly_maintenance = _env_bool("LPG_CACHE_WEEKLY_MAINTENANCE", False if self.render else True)

        # Purge policy: keep memory thread clean (LUCKY-only).
        # Default ON to match 'thread = memory' semantics.
        self.purge_nonlucky_on_boot = os.getenv('LPG_CACHE_PURGE_NONLUCKY_ON_BOOT', '1') == '1'
        self.purge_limit = _env_int('LPG_CACHE_PURGE_LIMIT', 0)  # 0 = use boot_scan_limit/unlimited
        self.purge_sleep_ms = _env_int('LPG_CACHE_PURGE_SLEEP_MS', 350)


        # Map for delete=unlearn
        self._msgid_to_sha1: Dict[int, str] = {}

        self.thread: Optional[discord.Thread] = None

        # Configure memory module now (must happen before cache hook uses it heavily)
        try:
            from nixe.helpers import lpg_cache_memory as cache
            cache.configure(self.cache_max_entries)
            log.warning(
                "[lpgmem] mode=%s cache_max_entries=%s boot_scan_limit=%s backfill_per_boot=%s weekly=%s",
                "render" if self.render else ("minipc" if self.minipc else "other"),
                self.cache_max_entries,
                self.boot_scan_limit,
                self.backfill_per_boot,
                self.weekly_maintenance,
            )
        except Exception as e:
            log.warning("[lpgmem] cache.configure failed: %r", e)

    @commands.Cog.listener()
    async def on_ready(self):
        # Run bootstrap once per process; tasks.loop will handle weekly maintenance
        if self.thread is None:
            await self._bind_thread()
            await self._purge_nonlucky_in_thread()
            await self._bootstrap_from_thread()
            if self.weekly_maintenance and self.minipc and not self._weekly.is_running():
                self._weekly.start()

    async def _bind_thread(self):
        try:
            ch = self.bot.get_channel(MEMORY_THREAD_ID) or await self.bot.fetch_channel(MEMORY_THREAD_ID)
            if isinstance(ch, discord.Thread):
                self.thread = ch
                log.warning("[lpgmem] bound thread=%s (%s)", ch.name, ch.id)
            else:
                self.thread = None
                log.warning("[lpgmem] channel %s is not a Thread (got %s)", MEMORY_THREAD_ID, type(ch).__name__)
        except Exception as e:
            self.thread = None
            log.warning("[lpgmem] bind thread failed: %r", e)


    async def _purge_nonlucky_in_thread(self):
        """Delete NOT LUCKY log messages from the permanent memory thread (bot-authored only)."""
        if not self.thread or not getattr(self.bot, 'user', None):
            return
        if not self.purge_nonlucky_on_boot:
            return
        # Decide scan limit
        limit = None
        try:
            if self.purge_limit and self.purge_limit > 0:
                limit = int(self.purge_limit)
            elif self.boot_scan_limit and self.boot_scan_limit > 0:
                limit = int(self.boot_scan_limit)
            else:
                limit = None
        except Exception:
            limit = None
        sleep_s = max(0.1, float(self.purge_sleep_ms or 0) / 1000.0)
        scanned = 0
        deleted = 0
        try:
            async for msg in self.thread.history(limit=limit, oldest_first=False):
                scanned += 1
                try:
                    if getattr(msg, 'author', None) and msg.author.id != self.bot.user.id:
                        continue
                except Exception:
                    pass
                emb = (msg.embeds[0] if getattr(msg, 'embeds', None) else None)
                if not emb:
                    continue
                is_not_lucky = False
                try:
                    for f in getattr(emb, 'fields', []) or []:
                        if str(getattr(f, 'name', '')).strip().lower() == 'result':
                            vv = str(getattr(f, 'value', '') or '').lower()
                            if ('not lucky' in vv) or ('❌' in str(getattr(f, 'value', '') or '')):
                                is_not_lucky = True
                            break
                except Exception:
                    is_not_lucky = False
                if not is_not_lucky:
                    continue
                try:
                    await msg.delete()
                    deleted += 1
                    await asyncio.sleep(sleep_s)
                except Exception as e:
                    log.warning('[lpgmem] purge delete failed mid=%s: %r', getattr(msg,'id','?'), e)
        except Exception as e:
            log.warning('[lpgmem] purge scan failed: %r', e)
        log.info('[lpgmem] purge_nonlucky scanned=%d deleted=%d limit=%s', scanned, deleted, str(limit))

    async def _bootstrap_from_thread(self):
        if not self.thread:
            return

        try:
            from nixe.helpers import lpg_cache_memory as cache
        except Exception as e:
            log.warning("[lpgmem] bootstrap: cannot import cache: %r", e)
            return

        loaded = 0
        backfilled = 0
        scanned = 0
        limit = None if self.boot_scan_limit <= 0 else int(self.boot_scan_limit)

        try:
            async for msg in self.thread.history(limit=limit, oldest_first=False):
                scanned += 1
                sha1 = ""
                ah = ""
                # Parse footer if present
                try:
                    emb = (msg.embeds[0] if msg.embeds else None)
                    footer_text = (getattr(getattr(emb, "footer", None), "text", "") if emb else "") or ""
                    m = _FOOTER_RE.search(footer_text)
                    if m:
                        sha1 = m.group(1).lower()
                        ah = m.group(2).lower()
                except Exception:
                    sha1 = ""
                    ah = ""

                if sha1 and ah:
                    # Upsert without downloading attachment
                    try:
                        score, provider, reason, _ph = _extract_fields_from_embed(msg.embeds[0]) if msg.embeds else (0.0, "-", "-", "-")
                        cache.upsert_entry(
                            {
                                "sha1": sha1,
                                "ahash": ah,
                                "ok": True,
                                "score": float(score),
                                "via": str(provider),
                                "reason": str(reason),
                                "w": 0,
                                "h": 0,
                                "ts": float(msg.created_at.timestamp()) if getattr(msg, "created_at", None) else 0.0,
                            }
                        )
                        self._msgid_to_sha1[int(msg.id)] = sha1
                        loaded += 1
                    except Exception:
                        continue
                    continue

                # No footer: optionally backfill by downloading attachment (bounded)
                if backfilled >= self.backfill_per_boot:
                    continue
                if not msg.attachments:
                    continue
                try:
                    att = msg.attachments[0]
                    # Hard safety: skip huge attachments on render
                    if self.render:
                        try:
                            if getattr(att, "size", 0) and int(att.size) > 1_200_000:
                                continue
                        except Exception:
                            pass
                    image_bytes = await att.read()
                    # Insert to cache (this computes sha1/ahash)
                    score, provider, reason, _ph = _extract_fields_from_embed(msg.embeds[0]) if msg.embeds else (0.0, "-", "-", "-")
                    ent = cache.put(image_bytes, True, float(score), str(provider), str(reason))
                    sha1 = str(ent.get("sha1") or "")
                    ah = str(ent.get("ahash") or "")
                    if sha1:
                        self._msgid_to_sha1[int(msg.id)] = sha1
                        loaded += 1
                    backfilled += 1

                    # Optional: write footer back for faster future boots (minipc default ON)
                    if self.backfill_write_footer and sha1 and ah and msg.embeds:
                        try:
                            emb0 = msg.embeds[0]
                            # Recreate embed to allow footer update (discord.py embeds are immutable-ish)
                            new_emb = discord.Embed.from_dict(emb0.to_dict())
                            new_emb.set_footer(text=f"lpgmem sha1={sha1} ahash={ah}")
                            await msg.edit(embed=new_emb)
                        except Exception:
                            pass
                except Exception:
                    continue

            log.warning(
                "[lpgmem] bootstrap scanned=%s loaded=%s backfilled=%s (render=%s minipc=%s)",
                scanned,
                loaded,
                backfilled,
                self.render,
                self.minipc,
            )
        except Exception as e:
            log.warning("[lpgmem] bootstrap failed: %r", e)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        # delete=unlearn (thread only)
        try:
            if int(getattr(payload, "channel_id", 0) or 0) != int(MEMORY_THREAD_ID):
                return
            mid = int(getattr(payload, "message_id", 0) or 0)
            sha1 = self._msgid_to_sha1.pop(mid, None)
            if not sha1:
                return
            from nixe.helpers import lpg_cache_memory as cache
            cache.remove_sha1(sha1)
            log.warning("[lpgmem] unlearn mid=%s sha1=%s", mid, sha1[:8])
        except Exception:
            return

    @tasks.loop(hours=24 * 7)
    async def _weekly(self):
        # Weekly maintenance is minipc-only by default.
        if not (self.weekly_maintenance and self.minipc):
            return
        if not self.thread:
            await self._bind_thread()
            if not self.thread:
                return
        # Backfill missing footers to keep future boots fast and deterministic.
        try:
            await self._bootstrap_from_thread()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(LPGCachePersistence(bot))