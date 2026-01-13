from __future__ import annotations
import asyncio
from typing import Tuple

from .lp_gemini_helper import is_gemini_enabled
from . import gemini_bridge

async def is_lucky_pull_async(
    image_bytes: bytes,
    threshold: float = 0.65,
    timeout: float = 7.0,
) -> Tuple[bool, float, str]:
    """Async LPG decision helper.

    Returns (decision, score, reason). Never uses GROQ_API_KEY, never uses Google Gemini REST.
    """
    if not is_gemini_enabled():
        return (False, 0.0, "lpg_disabled_or_missing_key")
    try:
        ok, score, _via, reason = await asyncio.wait_for(
            gemini_bridge.classify_lucky_pull_bytes(image_bytes),
            timeout=timeout,
        )
        score = float(score or 0.0)
        if not ok:
            return (False, score, reason or "not_ok")
        return (bool(score >= float(threshold)), score, str(reason or ""))
    except asyncio.TimeoutError:
        return (False, 0.0, "timeout")
    except Exception as e:
        return (False, 0.0, f"error:{e}")
