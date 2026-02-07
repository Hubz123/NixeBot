from __future__ import annotations

"""
[a19-phash-marker-safe]
Overlay to make pHash DB parser tolerant to marker/header lines.

This overlay patches:
- nixe.helpers.phash_board.edit_pinned_db: robust extraction of existing JSON payload
- nixe.cogs.status_commands._parse_phash_json: robust JSON extraction for status command

Additive and safe: NOOP if targets not found.
"""

import json
import logging
import re
from typing import Any, Dict, Optional, Iterable, Tuple

log = logging.getLogger(__name__)

_CODEBLOCK_JSON_RE = re.compile(r"```(?:json)?\s*({.*?})\s*```", re.DOTALL | re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"({\s*\"phash\"\s*:\s*\[.*?\]\s*})", re.DOTALL | re.IGNORECASE)

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        m = _CODEBLOCK_JSON_RE.search(text)
        if m:
            return json.loads(m.group(1))
    except Exception:
        pass
    try:
        m = _JSON_OBJECT_RE.search(text)
        if m:
            return json.loads(m.group(1))
    except Exception:
        pass
    try:
        m = re.search(r"\{[\s\S]*\}", text or "", re.M)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return None

def _safe_phash_list(obj: Optional[Dict[str, Any]]) -> list[str]:
    if not isinstance(obj, dict):
        return []
    arr = obj.get("phash") or []
    if not isinstance(arr, list):
        return []
    return [str(x) for x in arr if str(x)]

async def _edit_pinned_db_safe(pb, bot, tokens: Iterable[str]) -> bool:
    msg = await pb.get_pinned_db_message(bot)
    if not msg:
        return False

    text = msg.content or ""
    obj = _extract_json(text) or {"phash": []}

    existing = set(_safe_phash_list(obj))
    incoming = {str(t) for t in tokens if str(t)}
    merged = existing | incoming

    if not merged and existing:
        return True

    obj["phash"] = sorted(merged)

    new_json = json.dumps(obj, separators=(",", ":"))
    marker = getattr(pb, "PHASH_DB_MARKER", "NIXE_PHASH_DB_V1")
    new_content = f"{marker}\n```json\n{new_json}\n```\n"

    try:
        await msg.edit(content=new_content)
        try:
            if hasattr(pb, "set_phash_ids"):
                pb.set_phash_ids(thread_id=msg.channel.id, msg_id=msg.id)
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning("[phash-marker-safe] failed to edit DB message: %r", e)
        return False

def _patch_callable(obj, name: str, wrapper_factory) -> bool:
    try:
        orig = getattr(obj, name, None)
        if not callable(orig):
            return False
        if getattr(orig, "_phash_marker_safe_patched", False):
            return True
        wrapped = wrapper_factory(orig)
        setattr(wrapped, "_phash_marker_safe_patched", True)
        setattr(obj, name, wrapped)
        log.warning(f"[phash-marker-safe] patched {getattr(obj, '__name__', obj)}.{name}")
        return True
    except Exception:
        return False

async def setup(bot):
    patched_any = False

    # Patch helper: edit_pinned_db
    try:
        import nixe.helpers.phash_board as pb

        def wf(orig):
            async def inner(bot_obj, tokens):
                return await _edit_pinned_db_safe(pb, bot_obj, tokens)
            return inner

        if _patch_callable(pb, "edit_pinned_db", wf):
            patched_any = True
    except Exception as e:
        log.warning(f"[phash-marker-safe] helper patch skipped: {e}")

    # Patch status command parser (pure function)
    try:
        import nixe.cogs.status_commands as sc

        def wf2(orig):
            def inner(text: str) -> Tuple[int, Optional[dict]]:
                obj = _extract_json(text or "")
                if isinstance(obj, dict):
                    arr = obj.get("phash") or []
                    return (len(arr) if isinstance(arr, list) else 0, obj)
                return orig(text)
            return inner

        if _patch_callable(sc, "_parse_phash_json", wf2):
            patched_any = True
    except Exception as e:
        log.warning(f"[phash-marker-safe] status parser patch skipped: {e}")

    if not patched_any:
        log.warning("[phash-marker-safe] no targets patched (NOOP)")
