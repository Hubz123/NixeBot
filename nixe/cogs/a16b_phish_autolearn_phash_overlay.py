# -*- coding: utf-8 -*-
from __future__ import annotations

"""
[a16b-phish-autolearn-phash]
When Groq (or other phishing pipelines) confirms phishing, automatically compute pHash for evidence images
(<= 1MB by default) and merge into the pinned pHash DB so Nixe remembers the pattern next time.

Design goals:
- Strict size gate: only learn <= PHISH_PHASH_MAX_BYTES (default 1MB) to avoid heavy downloads and reduce risk.
- Rate-limit friendly: URL-level dedupe (TTL) and batch-commit tokens once per event.
- Backward compatible: uses the existing pinned DB message machinery (nixe.helpers.phash_board.edit_pinned_db).
"""

import os
import io
import logging
import asyncio
from typing import Iterable, List, Set

import aiohttp
from discord.ext import commands

from nixe.helpers.img_hashing import phash_list_from_bytes
from nixe.helpers.phash_board import edit_pinned_db
from nixe.helpers.once import once_sync as _once

log = logging.getLogger("nixe.cogs.a16b_phish_autolearn_phash_overlay")

def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return default

PHISH_PHASH_MAX_BYTES = _env_int("PHISH_PHASH_MAX_BYTES", _env_int("PHISH_IMAGE_MAX_BYTES", 1048576))
PHISH_AUTO_LEARN = (os.getenv("PHISH_AUTO_LEARN_PHASH", "1").strip().lower() in ("1","true","yes","on"))
PHISH_AUTO_LEARN_MIN_SCORE = float(os.getenv("PHISH_AUTO_LEARN_MIN_SCORE", "0.90"))
PHISH_AUTO_LEARN_TTL_SEC = _env_int("PHISH_AUTO_LEARN_TTL_SEC", 7 * 24 * 3600)
TIMEOUT_MS = _env_int("PHISH_AUTO_LEARN_TIMEOUT_MS", 4000)

async def _fetch_limited(sess: aiohttp.ClientSession, url: str, max_bytes: int) -> bytes:
    if not url:
        return b""
    try:
        async with sess.get(url, headers={"Range": f"bytes=0-{max_bytes}"} ) as r:
            if r.status >= 400:
                return b""
            data = await r.read()
            # If response exceeds max_bytes, Range might be ignored; hard enforce.
            if data and len(data) > max_bytes:
                return b""
            return data or b""
    except Exception:
        return b""

class PhishAutoLearnPhash(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener("on_nixe_phish_detected")
    async def on_nixe_phish_detected(self, payload: dict):
        if not PHISH_AUTO_LEARN or not isinstance(payload, dict):
            return
        try:
            score = float(payload.get("score", 0.0) or 0.0)
        except Exception:
            score = 0.0
        if score < PHISH_AUTO_LEARN_MIN_SCORE:
            return

        ev = payload.get("evidence") or []
        if not isinstance(ev, list) or not ev:
            return

        urls: List[str] = [str(u) for u in ev if u]
        if not urls:
            return

        # URL-level dedupe to prevent hammering CDN / editing DB repeatedly.
        urls = [u for u in urls if _once(f"phish-learn-url:{u}", ttl=PHISH_AUTO_LEARN_TTL_SEC)]
        if not urls:
            return

        tokens: Set[str] = set()
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_MS / 1000.0)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            for u in urls[:8]:
                raw = await _fetch_limited(sess, u, PHISH_PHASH_MAX_BYTES)
                if not raw:
                    continue
                try:
                    for h in phash_list_from_bytes(raw, max_frames=6):
                        if h:
                            tokens.add(str(h))
                except Exception:
                    continue

        if not tokens:
            return

        # Merge into pinned DB (single edit per event).
        try:
            ok = await edit_pinned_db(self.bot, tokens)
            if ok:
                log.warning("[phish-autolearn] learned %d phash token(s) from %d evidence url(s)", len(tokens), len(urls))
        except Exception as e:
            log.debug("[phish-autolearn] commit err: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(PhishAutoLearnPhash(bot))
