# -*- coding: utf-8 -*-
"""
LPG classify timeout shield (safe-load, runtime-aligned)

Objective:
- Prevent premature internal timeouts that cause: via=timeout reason=classify_timeout
- Keep behavior "seperti semula" (LPG guard controls overall timeout), while ensuring
  any internal shield does NOT cut below the runtime budget.

Notes:
- This module MUST NEVER break cog loading. If patching cannot be applied safely,
  it becomes idle (no-op).
- Does NOT modify runtime/config files. Reads runtime via env_reader.get().
- Can be disabled with LPG_SHIELD_ENABLE=0.

Runtime keys used:
- LPG_TIMEOUT_SEC (outer guard timeout budget, default 12.0)
Optional:
- LPG_SHIELD_MARGIN_SEC (default 0.7)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from discord.ext import commands

log = logging.getLogger(__name__)


def _to_float(v: Any, default: float) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        if not s:
            return default
        return float(s)
    except Exception:
        return default


def _outer_timeout_seconds() -> float:
    # Use env_reader so runtime_env.json is honored even when os.environ is not populated.
    try:
        from nixe.helpers.env_reader import get  # type: ignore
        return _to_float(get("LPG_TIMEOUT_SEC", 12.0), 12.0)
    except Exception:
        # Fallback to environment only (should not be needed in normal runs)
        import os
        return _to_float(os.getenv("LPG_TIMEOUT_SEC", "12.0"), 12.0)


def _shield_enabled() -> bool:
    try:
        from nixe.helpers.env_reader import get  # type: ignore
        v = str(get("LPG_SHIELD_ENABLE", "1")).strip()
    except Exception:
        import os
        v = str(os.getenv("LPG_SHIELD_ENABLE", "1")).strip()
    return v != "0"


def _shield_margin_seconds() -> float:
    try:
        from nixe.helpers.env_reader import get  # type: ignore
        return _to_float(get("LPG_SHIELD_MARGIN_SEC", 0.7), 0.7)
    except Exception:
        import os
        return _to_float(os.getenv("LPG_SHIELD_MARGIN_SEC", "0.7"), 0.7)


def _soft_timeout_seconds() -> float:
    outer = _outer_timeout_seconds()
    margin = _shield_margin_seconds()
    # Keep a small margin so the shield never "wins" against outer wait_for.
    # Ensure minimum reasonable time for vision/OCR.
    soft = max(3.0, outer - max(0.2, margin))
    # Never exceed outer (guard remains the final authority)
    if soft >= outer:
        soft = max(0.5, outer - 0.2)
    return soft


class LPGClassifyTimeoutShieldOverlay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._orig: Optional[Callable[..., Awaitable[Any]]] = None
        self._patched: bool = False

    async def cog_load(self) -> None:
        # Patching on load
        await self._maybe_patch()

    async def cog_unload(self) -> None:
        await self._restore()

    async def _maybe_patch(self) -> None:
        if not _shield_enabled():
            log.info("[lpg-shield] disabled via LPG_SHIELD_ENABLE=0")
            return
        if self._patched:
            return
        try:
            from nixe.helpers import gemini_bridge as gb  # type: ignore
        except Exception as e:
            log.warning("[lpg-shield] cannot import gemini_bridge: %r (shield idle)", e)
            return

        fn = getattr(gb, "classify_lucky_pull_bytes", None)
        if not callable(fn):
            log.warning("[lpg-shield] gemini_bridge.classify_lucky_pull_bytes missing (shield idle)")
            return

        self._orig = fn

        async def _shielded(image_bytes: bytes, *args: Any, **kwargs: Any) -> Any:
            # Do NOT shrink budgets here; only enforce a soft ceiling aligned to runtime.
            timeout_s = _soft_timeout_seconds()
            try:
                return await asyncio.wait_for(self._orig(image_bytes, *args, **kwargs), timeout=timeout_s)  # type: ignore[misc]
            except asyncio.TimeoutError:
                # Preserve original timeout behavior (guard will handle retries/fallback)
                raise

        gb.classify_lucky_pull_bytes = _shielded  # type: ignore[assignment]
        self._patched = True
        log.info("[lpg-shield] patched classify_lucky_pull_bytes (soft_timeout=%.2fs outer=%.2fs)",
                 _soft_timeout_seconds(), _outer_timeout_seconds())

    async def _restore(self) -> None:
        if not self._patched:
            return
        try:
            from nixe.helpers import gemini_bridge as gb  # type: ignore
            if self._orig is not None and callable(getattr(gb, "classify_lucky_pull_bytes", None)):
                gb.classify_lucky_pull_bytes = self._orig  # type: ignore[assignment]
                log.info("[lpg-shield] restored original classify_lucky_pull_bytes")
        except Exception:
            pass
        self._orig = None
        self._patched = False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LPGClassifyTimeoutShieldOverlay(bot))
