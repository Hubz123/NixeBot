#!/usr/bin/env python3
import os, logging, asyncio, discord
from typing import Optional

BASE_ID = int(os.getenv("LPG_CACHE_THREAD_ID", "1431178130155896882"))
NAME = os.getenv("LPG_THREAD_NAME", "nixe-lpg-sticky")
AUTO_ARCHIVE = int(os.getenv("LPG_THREAD_AUTO_ARCHIVE_MIN", "10080"))

async def _find_existing_in_text(parent: discord.TextChannel) -> Optional[discord.Thread]:
    for th in parent.threads:
        if th.name == NAME:
            return th
    try:
        async for th in parent.archived_threads(limit=100, private=False):
            if th.name == NAME:
                return th
    except Exception as e:
        logging.warning("[lpg-thread] archived scan failed: %s", e)
    return None

async def ensure_sticky_thread(bot: discord.Client) -> Optional[discord.Thread]:
    try:
        ch = await bot.fetch_channel(BASE_ID)
    except Exception as e:
        logging.error("[lpg-thread] fetch_channel(%s) failed: %s", BASE_ID, e)
        return None
    if isinstance(ch, discord.Thread):
        if ch.archived:
            try: await ch.edit(archived=False, reason="revive lpg sticky")
            except Exception as e: logging.warning("[lpg-thread] unarchive failed: %s", e)
        return ch
    if isinstance(ch, discord.TextChannel):
        existing = await _find_existing_in_text(ch)
        if existing:
            if existing.archived:
                try: await existing.edit(archived=False, reason="revive lpg sticky")
                except Exception as e: logging.warning("[lpg-thread] revive failed: %s", e)
            return existing
        try:
            th = await ch.create_thread(
                name=NAME,
                auto_archive_duration=AUTO_ARCHIVE,
                reason="Nixe: Lucky Pull cache/whitelist sticky thread"
            )
            logging.info("[lpg-thread] created thread '%s' (id=%s) under #%s", NAME, th.id, ch.id)
            return th
        except Exception as e:
            logging.error("[lpg-thread] create_thread failed: %s", e)
            return None
    logging.error("[lpg-thread] BASE_ID %s is not a Thread/TextChannel (got %s)", BASE_ID, type(ch).__name__)
    return None

async def ensure_single_pinned(th: discord.Thread, marker: str):
    try:
        pins = await th.pins()
    except Exception as e:
        logging.warning("[lpg-thread] fetch pins failed: %s", e); pins = []
    target = None
    for m in pins:
        if marker in (m.content or ""):
            target = m; break
    if target is None:
        try:
            msg = await th.send(f"{marker}\n**Lucky Pull Cache / Whitelist Sticky**\n(autoupdated)")
            try: await msg.pin()
            except Exception: pass
            logging.info("[lpg-thread] created & pinned sticky message id=%s", msg.id)
            target = msg
        except Exception as e:
            logging.error("[lpg-thread] send/pin sticky failed: %s", e)
            return None
    return target
