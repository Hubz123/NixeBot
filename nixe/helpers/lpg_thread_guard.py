#!/usr/bin/env python3
# nixe/helpers/lpg_thread_guard.py (v15)
# Bot-scoped singleton: prevents duplicate thread creation across multiple cogs.
import os, asyncio, logging
from typing import Optional, List
import discord
def _cfg_name() -> str:
    return os.getenv("LPG_THREAD_NAME", "nixe-lpg-sticky")
def _cfg_parent_id() -> int:
    # Never fallback to Lucky Pull; use dedicated IDs only.
    for key in ("LPG_CACHE_THREAD_ID", "LPG_PARENT_CHANNEL_ID", "LPG_WHITELIST_PARENT_CHANNEL_ID"):
        v = os.getenv(key)
        if v:
            try:
                vid = int(str(v).strip())
                if vid > 0:
                    return vid
            except Exception:
                continue
    return 0

def _cfg_auto_dedup() -> bool:
    return os.getenv("LPG_DEDUP_CLOSE", "1") == "1"
async def _list_threads(parent: discord.TextChannel) -> List[discord.Thread]:
    out = list(parent.threads)
    try:
        async for th in parent.archived_threads(limit=100, private=False):
            out.append(th)
    except Exception as e:
        logging.debug("[lpg-thread] archived scan failed: %s", e)
    return out
async def _pick_existing(parent: discord.TextChannel, name: str) -> Optional[discord.Thread]:
    items = await _list_threads(parent)
    candidates = [th for th in items if th.name == name]
    if not candidates:
        return None
    chosen = sorted(candidates, key=lambda t: t.id)[0]
    if _cfg_auto_dedup() and len(candidates) > 1:
        for th in candidates[1:]:
            try:
                if not th.archived:
                    await th.edit(archived=True, reason="Nixe: LPG sticky dedup")
                    logging.info("[lpg-thread] archived duplicate '%s' id=%s", th.name, th.id)
            except Exception as e:
                logging.debug("[lpg-thread] dedup failed for %s: %s", th.id, e)
    return chosen
def _get_bot_slots(bot) -> tuple:
    if not hasattr(bot, "_lpg_thread_lock"):
        bot._lpg_thread_lock = asyncio.Lock()
    if not hasattr(bot, "_lpg_thread_ready"):
        bot._lpg_thread_ready = asyncio.Event()
    if not hasattr(bot, "_lpg_thread_id"):
        bot._lpg_thread_id = None
    return bot._lpg_thread_lock, bot._lpg_thread_ready
async def ensure_sticky_thread(bot: discord.Client) -> Optional[discord.Thread]:
    name = _cfg_name()
    parent_id = _cfg_parent_id()
    if not parent_id:
        logging.error("[lpg-thread] LPG_CACHE_THREAD_ID missing/invalid")
        return None
    lock, ready = _get_bot_slots(bot)
    if getattr(bot, "_lpg_thread_id", None):
        try:
            th = await bot.fetch_channel(bot._lpg_thread_id)
            if isinstance(th, discord.Thread):
                if th.archived:
                    try:
                        await th.edit(archived=False, reason="revive (cached)")
                    except Exception:
                        pass
                return th
        except Exception:
            bot._lpg_thread_id = None
    async with lock:
        if getattr(bot, "_lpg_thread_id", None):
            try:
                th = await bot.fetch_channel(bot._lpg_thread_id)
                if isinstance(th, discord.Thread):
                    if th.archived:
                        try:
                            await th.edit(archived=False, reason="revive (locked)")
                        except Exception:
                            pass
                    ready.set()
                    return th
            except Exception:
                bot._lpg_thread_id = None
        try:
            ch = await bot.fetch_channel(parent_id)
        except Exception as e:
            logging.error("[lpg-thread] fetch_channel(%s) failed: %s", parent_id, e)
            return None
        if isinstance(ch, discord.Thread):
            if ch.archived:
                try:
                    await ch.edit(archived=False, reason="revive base thread")
                except Exception:
                    pass
            bot._lpg_thread_id = ch.id
            ready.set()
            return ch
        if not isinstance(ch, discord.TextChannel):
            logging.error("[lpg-thread] base id %s is not TextChannel/Thread (got %s)", parent_id, type(ch).__name__)
            return None
        existing = await _pick_existing(ch, name)
        if existing:
            if existing.archived:
                try:
                    await existing.edit(archived=False, reason="revive existing")
                except Exception:
                    pass
            bot._lpg_thread_id = existing.id
            ready.set()
            return existing
        try:
            th = await ch.create_thread(
                name=name,
                auto_archive_duration=int(os.getenv("LPG_THREAD_AUTO_ARCHIVE_MIN", "10080")),
                reason="Nixe: LPG sticky thread",
            )
            bot._lpg_thread_id = th.id
            logging.info("[lpg-thread] created thread '%s' (id=%s) under #%s", name, th.id, ch.id)
            ready.set()
            return th
        except Exception as e:
            logging.error("[lpg-thread] create_thread failed: %s", e)
            return None
async def ensure_single_pinned(th: discord.Thread, marker: str) -> Optional[discord.Message]:
    try:
        pins = await th.pins()
    except Exception as e:
        logging.warning("[lpg-thread] fetch pins failed: %s", e)
        pins = []
    for m in pins:
        if marker in (m.content or ""):
            return m
    try:
        msg = await th.send(f"{marker}\n**Lucky Pull Sticky** (auto-updated)")
        try:
            await msg.pin()
        except Exception:
            pass
        logging.info("[lpg-thread] created & pinned sticky message id=%s", msg.id)
        return msg
    except Exception as e:
        logging.error("[lpg-thread] send/pin sticky failed: %s", e)
        return None
