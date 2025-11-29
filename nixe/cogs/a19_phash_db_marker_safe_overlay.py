from __future__ import annotations

"""
[a19-phash-marker-safe]
Overlay to make pHash DB parser tolerant to marker/header lines.
Pinned DB message with a marker + json codeblock will be parsed correctly.

Additive and safe: NOOP if targets not found.
"""

import json
import logging
import re
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

_CODEBLOCK_JSON_RE = re.compile(r"```(?:json)?\s*({.*?})\s*```", re.DOTALL | re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"({\s*\"phash\"\s*:\s*\[.*?\]\s*})", re.DOTALL | re.IGNORECASE)

def extract_phash_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    m = _CODEBLOCK_JSON_RE.search(text)
    if m:
        blob = m.group(1)
        try:
            return json.loads(blob)
        except Exception:
            pass

    m2 = _JSON_OBJECT_RE.search(text)
    if m2:
        blob = m2.group(1)
        try:
            return json.loads(blob)
        except Exception:
            pass

    # fallback: strip marker-like lines then try
    lines = [ln for ln in text.splitlines() if ln.strip()]
    stripped = "\n".join([ln for ln in lines if not ln.strip().startswith("NIXE_PHASH_DB")])
    try:
        return json.loads(stripped)
    except Exception:
        return None

def _patch_callable(obj: Any, name: str) -> bool:
    fn = getattr(obj, name, None)
    if not callable(fn):
        return False
    if getattr(fn, "_nixe_marker_safe_patched", False):
        return True

    def wrapped(text: str, *args, **kwargs):
        data = extract_phash_json(text)
        if data is not None:
            return data
        return fn(text, *args, **kwargs)

    setattr(wrapped, "_nixe_marker_safe_patched", True)
    setattr(obj, name, wrapped)
    log.warning(f"[phash-marker-safe] patched {obj.__name__}.{name}")
    return True

async def setup(bot):
    patched = False
    try:
        import nixe.cogs.phash_phish_guard as mod
    except Exception as e:
        log.warning(f"[phash-marker-safe] import phash_phish_guard failed: {e}")
        return

    candidates = [
        "parse_db_message",
        "_parse_db_message",
        "parse_phash_db_message",
        "_parse_phash_db_message",
    ]
    for n in candidates:
        if _patch_callable(mod, n):
            patched = True
            break

    if not patched:
        for cls_name in ["PHashPhishGuard", "PHashPhishGuardCog", "PHashPhishGuardOverlay"]:
            cls = getattr(mod, cls_name, None)
            if cls and hasattr(cls, "__dict__"):
                for n in ["_parse_db_message", "parse_db_message", "_load_db_from_message"]:
                    if _patch_callable(cls, n):
                        patched = True
                        break
            if patched:
                break

    if not patched:
        log.warning("[phash-marker-safe] no known parser target found; NOOP")
