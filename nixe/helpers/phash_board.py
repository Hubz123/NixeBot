from __future__ import annotations

from typing import Optional, Dict, Any, Set, Iterable
from pathlib import Path
import os
import json
import logging
import asyncio

import discord

from nixe.state_runtime import get_phash_ids, set_phash_ids


log = logging.getLogger("nixe.helpers.phash_board")

PHASH_DB_MARKER = os.getenv("PHASH_DB_MARKER", "NIXE_PHASH_DB_V1").strip() or "NIXE_PHASH_DB_V1"


def ensure_phash_board(*args, **kwargs) -> Dict[str, Any]:
    # Legacy stub kept for compatibility with older cogs.
    mid = int(str(kwargs.get("message_id", 0)) or 0)
    return {"ok": True, "message_id": mid}


def update_phash_board(*args, **kwargs) -> Dict[str, Any]:
    # Legacy stub kept for compatibility with older cogs.
    return {"ok": True}


def find_phash_db_message(*args, **kwargs) -> Optional[int]:
    # Legacy stub kept for compatibility with older overlays.
    return int(str(kwargs.get("message_id", 0)) or 0)


def get_blacklist_hashes() -> Set[int]:
    out: Set[int] = set()
    p = Path(__file__).resolve().parents[1] / "data" / "phash_blacklist.txt"
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip().lower()
            if not s:
                continue
            try:
                out.add(int(s, 16) if s.startswith("0x") else int(s))
            except Exception:
                # ignore malformed lines
                pass
    except Exception:
        # file missing or unreadable
        pass
    return out


async def discover_db_message_id(bot) -> int:
    try:
        from nixe import config_phash as _cfg

        return int(getattr(_cfg, "PHASH_DB_MESSAGE_ID", 0) or 0)
    except Exception:
        return 0


def looks_like_phash_db(content: str) -> bool:
    if not content:
        return False
    s = str(content).lower()
    if PHASH_DB_MARKER.lower() in s:
        return True
    return ("phash" in s and "db" in s) or ("token" in s and "hash" in s) or ("blacklist" in s)


async def _load_template() -> str:
    # Try to load pinned_phash_db_template.txt, fall back to a minimal template.
    try:
        root = Path(__file__).resolve().parents[2]
        tpath = root / "data_templates" / "pinned_phash_db_template.txt"
        text = tpath.read_text(encoding="utf-8")
        if text.strip():
            return text
    except Exception:
        pass
    return f"{PHASH_DB_MARKER}\n```json\n{{\"phash\":[]}}\n```"


async def get_pinned_db_message(bot) -> Optional[discord.Message]:
    """Locate or create the pinned pHash DB message.

    Behaviour:
    - Prefer IDs from state_runtime / env (PHASH_DB_THREAD_ID / PHASH_DB_MESSAGE_ID).
    - If the message does not exist, search pins and recent history for a message
      that contains the PHASH_DB_MARKER.
    - If still not found, create a new DB message in the configured DB thread
      using the pinned_phash_db_template.txt content, then pin it.
    """
    try:
        tid, mid = get_phash_ids()
        thread_id = tid or int(os.getenv("PHASH_DB_THREAD_ID", "0") or 0)
        msg_id = mid or int(os.getenv("PHASH_DB_MESSAGE_ID", "0") or 0)
    except Exception:
        thread_id = int(os.getenv("PHASH_DB_THREAD_ID", "0") or 0)
        msg_id = int(os.getenv("PHASH_DB_MESSAGE_ID", "0") or 0)

    if not thread_id:
        return None

    try:
        chan = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
    except Exception as e:
        log.warning("phash-board: cannot resolve DB thread %s: %r", thread_id, e)
        return None

    if not isinstance(chan, (discord.Thread, discord.TextChannel)):
        return None

    # 1) Direct fetch by msg_id if we have one.
    if msg_id:
        try:
            msg = await chan.fetch_message(msg_id)
            if msg:
                return msg
        except Exception:
            pass

    # 2) Check pinned messages for an existing DB payload.
    try:
        pins = await chan.pins()
    except Exception:
        pins = []
    for m in pins:
        try:
            if m.author and bot.user and m.author.id == bot.user.id and looks_like_phash_db(m.content or ""):
                set_phash_ids(thread_id=chan.id, msg_id=m.id)
                return m
        except Exception:
            continue

    # 3) Scan recent history for a DB marker message.
    try:
        async for m in chan.history(limit=50):
            try:
                if m.author and bot.user and m.author.id == bot.user.id and looks_like_phash_db(m.content or ""):
                    try:
                        await m.pin()
                    except Exception:
                        pass
                    set_phash_ids(thread_id=chan.id, msg_id=m.id)
                    return m
            except Exception:
                continue
    except Exception:
        pass

    # 4) No existing DB message; create one from template.
    template = await _load_template()
    if PHASH_DB_MARKER not in template:
        template = f"{PHASH_DB_MARKER}\n```json\n{{\"phash\":[]}}\n```"

    try:
        msg = await chan.send(template)
        await asyncio.sleep(0.2)
        try:
            await msg.pin()
        except Exception:
            pass
        set_phash_ids(thread_id=chan.id, msg_id=msg.id)
        return msg
    except Exception as e:
        log.warning("phash-board: failed to create DB message in %s: %r", getattr(chan, "id", "<?>"), e)
        return None


async def edit_pinned_db(bot, tokens: Iterable[str]) -> bool:
    """Update the pinned DB message JSON payload with the given tokens.

    Tokens are stored as strings in the "phash" array.
    """
    msg = await get_pinned_db_message(bot)
    if not msg:
        return False

    text = msg.content or ""
    data: Dict[str, Any] = {"phash": []}

    # Extract existing JSON payload if present.
    try:
        s = text.find("```json")
        if s >= 0:
            s = text.find("{", s)
            e = text.find("```", s)
            if s >= 0 and e > s:
                data = json.loads(text[s:e])
    except Exception:
        data = {"phash": []}

    arr = data.get("phash") or []
    if not isinstance(arr, list):
        arr = []

    existing = {str(x) for x in arr}
    incoming = {str(t) for t in tokens if str(t)}

    merged = existing | incoming
    if not merged and existing:
        # nothing to update
        return True

    data["phash"] = sorted(merged)

    new_json = json.dumps(data, separators=(",", ":"))
    new_content = f"{PHASH_DB_MARKER}\n```json\n{new_json}\n```"

    try:
        await msg.edit(content=new_content)
        set_phash_ids(thread_id=msg.channel.id, msg_id=msg.id)
        return True
    except Exception as e:
        log.warning("phash-board: failed to edit DB message: %r", e)
        return False
