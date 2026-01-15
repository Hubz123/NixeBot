# -*- coding: utf-8 -*-
"""
Legacy compatibility helper used by LuckyPullGuard.

IMPORTANT RULES (project policy):
- LPG must use LPG_API_KEY / LPG_API_KEY_B (Groq keys). Legacy GEMINI_API_KEY/GEMINI_API_KEY_B is still accepted for backward compatibility.
- GROQ_API_KEY is phishing-only (do not rely on it for LPG).
- TRANSLATE_GEMINI_API_KEY is translate-only.

This module MUST NOT call Google Gemini REST.
"""

from __future__ import annotations

import os
import asyncio
from typing import Optional, Tuple

from .env_reader import get
from . import gemini_bridge

def _has_lpg_key() -> bool:
    # Prefer new naming
    if (get("LPG_API_KEYS", "") or "").strip():
        return True
    if (get("LPG_API_KEY", "") or "").strip():
        return True
    if (get("LPG_API_KEY_B", "") or "").strip():
        return True
    if (get("LPG_BACKUP_API_KEY", "") or "").strip():
        return True
    # Legacy fallback
    if (get("GEMINI_API_KEYS", "") or "").strip():
        return True
    if (get("GEMINI_API_KEY", "") or "").strip():
        return True
    if (get("GEMINI_API_KEY_B", "") or "").strip():
        return True
    if (get("GEMINI_BACKUP_API_KEY", "") or "").strip():
        return True
    return False

def is_gemini_enabled() -> bool:
    # Keep the existing env flag name for backward compatibility.
    return get("LUCKYPULL_GEMINI_ENABLE", "1") == "1" and _has_lpg_key()

async def score_lucky_pull_image_async(
    image_bytes: bytes,
    timeout: float = 12.0,
) -> Optional[Tuple[bool, float, str]]:
    """Async LPG scoring wrapper.
    Returns (is_lucky, score, reason) or None if unavailable/error.
    """
    if not is_gemini_enabled():
        return None
    try:
        ok, score, _via, reason = await asyncio.wait_for(
            gemini_bridge.classify_lucky_pull_bytes(image_bytes),
            timeout=timeout,
        )
        if not ok:
            return None
        return (bool(score >= 0.0), float(score), str(reason or "")[:200])
    except asyncio.TimeoutError:
        try:
            _t2 = max(float(timeout or 0.0) * 2.0, float(timeout or 0.0) + 2.0)
            _t2 = min(_t2, float(os.getenv('LPG_CLASSIFY_TIMEOUT_RETRY_CAP', '25') or '25'))
            ok, score, _via, reason = await asyncio.wait_for(
                gemini_bridge.classify_lucky_pull_bytes(image_bytes),
                timeout=_t2,
            )
            if not ok:
                return None
            return (bool(score >= 0.0), float(score), str(reason or '')[:200])
        except Exception:
            return None
    except Exception:
        return None

def score_lucky_pull_image(
    image_bytes: bytes,
    timeout: float = 12.0,
) -> Optional[Tuple[bool, float, str]]:
    """Synchronous wrapper.

    Safe to call ONLY when no event loop is running (e.g., in a worker thread).
    If called from within an active asyncio loop, returns None to avoid blocking.
    """
    if not is_gemini_enabled():
        return None
    try:
        asyncio.get_running_loop()
        # In event loop: do not block. Use score_lucky_pull_image_async instead.
        return None
    except RuntimeError:
        # No running loop: OK to run.
        return asyncio.run(score_lucky_pull_image_async(image_bytes, timeout=timeout))

def is_lucky_pull(image_bytes: bytes, threshold: float = 0.65):
    """Legacy interface returning (decision, score, reason)."""
    res = score_lucky_pull_image(image_bytes)
    if not res:
        return (False, 0.0, "lpg_unavailable_or_async_required")
    _ok, score, reason = res
    score = float(score or 0.0)
    return (bool(score >= float(threshold)), score, reason)
