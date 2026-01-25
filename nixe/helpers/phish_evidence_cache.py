# -*- coding: utf-8 -*-
"""nixe.helpers.phish_evidence_cache

Purpose
-------
Provide *human-readable* evidence (URL / attachment / embed preview / snippet)
for ban embeds triggered by FirstTouchdown phishing enforcement.

Design constraints (user requirement):
- Do NOT change runtime_env.json or other config files.
- Do NOT change ban decision flow; evidence is best-effort and non-blocking.
- Do NOT expose hash-based signals (pHash/aHash) as evidence.

Storage
-------
In-memory, short TTL cache keyed by (guild_id, user_id). This is sufficient
for Render free plan (single process) and for the typical "ban follows detection"
flow.

If cache is missing/expired, ban embed stays unchanged.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

# (guild_id, user_id) -> entry
_CACHE: Dict[Tuple[int, int], Dict[str, Any]] = {}

# Default TTL: 20 minutes (enough to cover audit-log delays)
_TTL_SEC: int = 20 * 60

# Strip hash-only tokens from evidence (avoid phash/ahash exposure)
_HASH_ONLY_RE = re.compile(r"^[0-9a-fA-F]{16,64}$")

_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}]+", flags=re.IGNORECASE)

_IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|webp|gif)(\?.*)?$", flags=re.IGNORECASE)


def _now() -> float:
    return time.time()


def _clean_list(items: List[str], limit: int = 6) -> List[str]:
    out: List[str] = []
    for x in items or []:
        if not isinstance(x, str):
            continue
        s = x.strip()
        if not s:
            continue
        # Drop pure hash-like tokens (phash/ahash/sha-like)
        if _HASH_ONLY_RE.match(s):
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def extract_urls_from_text(text: str) -> List[str]:
    if not text:
        return []
    return _clean_list(_URL_RE.findall(str(text) or ""), limit=8)


def _pick_image_url(candidates: List[str]) -> Optional[str]:
    for u in candidates or []:
        if not isinstance(u, str):
            continue
        s = u.strip()
        if not s:
            continue
        # accept 'filename | url' patterns
        if '|' in s and 'http' in s:
            try:
                s = s.split('|', 1)[1].strip()
            except Exception:
                pass
        if not s:
            continue
        if _IMAGE_EXT_RE.search(s):
            return s
        # Discord CDN attachments commonly have no extension in some cases; still allow
        # if it looks like an attachment URL.
        if 'cdn.discordapp.com/attachments/' in s or 'media.discordapp.net/attachments/' in s:
            return s
    return None


def _cleanup() -> None:
    try:
        now = _now()
        dead = [k for k, v in _CACHE.items() if now - float(v.get("ts", 0.0)) > _TTL_SEC]
        for k in dead:
            _CACHE.pop(k, None)
    except Exception:
        return


def clear_all() -> None:
    """Drop all in-memory evidence (best-effort)."""
    try:
        _CACHE.clear()
    except Exception:
        return


def record(
    guild_id: int,
    user_id: int,
    *,
    channel_id: int = 0,
    message_id: int = 0,
    jump_url: str = "",
    snippet: str = "",
    urls: Optional[List[str]] = None,
    attachments: Optional[List[str]] = None,
    embeds: Optional[List[str]] = None,
    provider: str = "",
    reason: str = "",
) -> None:
    """Record best-effort evidence for a (guild_id, user_id) pair."""
    try:
        gid = int(guild_id or 0)
        uid = int(user_id or 0)
        if gid <= 0 or uid <= 0:
            return
        _cleanup()

        # Normalize / cap
        snippet = (snippet or "").strip().replace("\n", " ")
        if len(snippet) > 220:
            snippet = snippet[:217] + "…"

        url_list = _clean_list(urls or [], limit=6)
        att_list = _clean_list(attachments or [], limit=6)
        emb_list = _clean_list(embeds or [], limit=4)

        # Auto build jump_url if missing
        if not jump_url and gid and channel_id and message_id:
            jump_url = f"https://discord.com/channels/{gid}/{int(channel_id)}/{int(message_id)}"

        # Merge into existing entry (prefer richer data)
        key = (gid, uid)
        prev = _CACHE.get(key) or {}

        merged_urls = _clean_list(list(dict.fromkeys((prev.get("urls") or []) + url_list)), limit=8)
        merged_atts = _clean_list(list(dict.fromkeys((prev.get("attachments") or []) + att_list)), limit=8)
        merged_embs = _clean_list(list(dict.fromkeys((prev.get("embeds") or []) + emb_list)), limit=6)

        merged_snip = snippet or (prev.get("snippet") or "")
        merged_jump = jump_url or (prev.get("jump_url") or "")

        _CACHE[key] = {
            "ts": _now(),
            "guild_id": gid,
            "user_id": uid,
            "channel_id": int(channel_id or prev.get("channel_id") or 0),
            "message_id": int(message_id or prev.get("message_id") or 0),
            "jump_url": merged_jump,
            "snippet": merged_snip,
            "urls": merged_urls,
            "attachments": merged_atts,
            "embeds": merged_embs,
            "image_url": _pick_image_url(merged_atts + merged_urls + merged_embs) or prev.get("image_url"),
            "provider": (provider or prev.get("provider") or "")[:48],
            "reason": (reason or prev.get("reason") or "")[:160],
        }
    except Exception:
        return


def record_from_payload(payload: Dict[str, Any], *, provider: str = "", reason: str = "") -> None:
    """Record evidence based on internal event payloads."""
    try:
        gid = int(payload.get("guild_id") or 0)
        uid = int(payload.get("user_id") or 0)
        cid = int(payload.get("channel_id") or 0)
        mid = int(payload.get("message_id") or 0)

        ev = payload.get("evidence") or []
        ev = [str(x) for x in ev if isinstance(x, (str, int, float))]
        urls = []
        atts = []
        # Heuristic: treat strings containing http as urls/attachments
        for s in ev:
            s2 = str(s).strip()
            if not s2:
                continue
            if "http://" in s2 or "https://" in s2:
                # Keep as evidence; split filename|url patterns are fine
                if "|" in s2 and "http" in s2:
                    atts.append(s2)
                else:
                    urls.append(s2)
            else:
                # Non-url evidence isn't very useful; ignore.
                pass

        jump = payload.get("jump_url") or ""
        if not jump and gid and cid and mid:
            jump = f"https://discord.com/channels/{gid}/{cid}/{mid}"

        record(
            gid,
            uid,
            channel_id=cid,
            message_id=mid,
            jump_url=str(jump),
            snippet=str(payload.get("snippet") or ""),
            urls=urls,
            attachments=atts,
            embeds=[],
            provider=provider or str(payload.get("provider") or ""),
            reason=reason or str(payload.get("reason") or ""),
        )
    except Exception:
        return


def record_message(message: Any, *, provider: str = "", reason: str = "") -> None:
    """Record evidence from a Discord message object (duck-typed)."""
    try:
        gid = int(getattr(getattr(message, "guild", None), "id", 0) or 0)
        cid = int(getattr(getattr(message, "channel", None), "id", 0) or 0)
        mid = int(getattr(message, "id", 0) or 0)
        author = getattr(message, "author", None)
        uid = int(getattr(author, "id", 0) or 0)

        jump = getattr(message, "jump_url", "") or ""
        if not jump and gid and cid and mid:
            jump = f"https://discord.com/channels/{gid}/{cid}/{mid}"

        content = str(getattr(message, "content", "") or "")
        snippet = content.strip()

        urls = extract_urls_from_text(content)

        attachments: List[str] = []
        for a in (getattr(message, "attachments", None) or []):
            try:
                fn = str(getattr(a, "filename", "") or "").strip()
                url = str(getattr(a, "url", "") or "").strip()
                if url:
                    attachments.append(f"{fn} | {url}" if fn else url)
            except Exception:
                continue

        embeds: List[str] = []
        for e in (getattr(message, "embeds", None) or []):
            try:
                u = str(getattr(e, "url", "") or "").strip()
                if u:
                    embeds.append(u)
                # thumbnail/image URLs
                img = getattr(e, "image", None)
                thumb = getattr(e, "thumbnail", None)
                for obj in (img, thumb):
                    try:
                        uu = str(getattr(obj, "url", "") or "").strip()
                        if uu:
                            embeds.append(uu)
                    except Exception:
                        pass
            except Exception:
                continue

        record(
            gid,
            uid,
            channel_id=cid,
            message_id=mid,
            jump_url=str(jump),
            snippet=snippet,
            urls=urls,
            attachments=attachments,
            embeds=embeds,
            provider=provider,
            reason=reason,
        )
    except Exception:
        return


def pop(guild_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    """Pop evidence for a given (guild_id, user_id)."""
    try:
        _cleanup()
        key = (int(guild_id or 0), int(user_id or 0))
        ent = _CACHE.pop(key, None)
        if not ent:
            return None
        # Ensure not expired
        if _now() - float(ent.get("ts", 0.0)) > _TTL_SEC:
            return None
        return ent
    except Exception:
        return None


def peek(guild_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    try:
        _cleanup()
        ent = _CACHE.get((int(guild_id or 0), int(user_id or 0)))
        if not ent:
            return None
        if _now() - float(ent.get("ts", 0.0)) > _TTL_SEC:
            return None
        return dict(ent)
    except Exception:
        return None
