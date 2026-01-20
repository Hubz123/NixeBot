# -*- coding: utf-8 -*-
"""nixe.helpers.lpg_denylist

Thread-backed denylist for LPG (Lucky Pull Guard).

Required behavior (user requirement):
- If the user deletes a message in the LPG memory thread that represents a LUCKY pull,
  the same image MUST be treated as NOT LUCKY on subsequent posts.

Persistence:
- Render uses ephemeral filesystem, so persistence must be via a Discord thread.
- This module only stores the in-process deny sets.
- The denylist thread manager cog is responsible for loading/saving.
"""

from __future__ import annotations

from typing import Iterable, Optional, Tuple


# sha1 (40 hex) -> denied
_DENY_SHA1: set[str] = set()

# ahash (16 hex) -> denied (exact bucket)
_DENY_AHASH: set[str] = set()


def _is_valid_ahash(a: str) -> bool:
    """Valid aHash is 16 hex chars and not the all-zero sentinel."""
    try:
        s = str(a or '').strip().lower()
        if len(s) != 16:
            return False
        if s == '0' * 16:
            return False
        int(s, 16)
        return True
    except Exception:
        return False



def clear() -> None:
    _DENY_SHA1.clear()
    _DENY_AHASH.clear()


def add(sha1: str, ahash: str | None = None) -> None:
    s = (sha1 or "").strip().lower()
    if s:
        _DENY_SHA1.add(s)
    a = (ahash or "").strip().lower()
    if _is_valid_ahash(a):
        _DENY_AHASH.add(a)


def add_many(items: Iterable[Tuple[str, str]]) -> None:
    for sha1, ah in items:
        add(sha1, ah)


def is_denied_sha1(sha1: str) -> bool:
    s = (sha1 or "").strip().lower()
    return bool(s) and s in _DENY_SHA1


def is_denied_ahash(ahash: str) -> bool:
    a = (ahash or "").strip().lower()
    if not _is_valid_ahash(a):
        return False
    return a in _DENY_AHASH


def stats() -> dict:
    return {"sha1": len(_DENY_SHA1), "ahash": len(_DENY_AHASH)}
