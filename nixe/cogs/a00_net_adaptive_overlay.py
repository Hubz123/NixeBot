# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp
from discord.ext import commands, tasks

from nixe.helpers import adaptive_limits as _al

log = logging.getLogger(__name__)

def _env_float(key: str, default: float) -> float:
    try:
        v = os.getenv(key)
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)

PROBE_SECONDS = _env_float("NIXE_NET_ADAPTIVE_PROBE_SECONDS", 30.0)
TIMEOUT_SECONDS = _env_float("NIXE_NET_ADAPTIVE_PROBE_TIMEOUT_SECONDS", 5.0)

GATEWAY_URL = os.getenv("NIXE_NET_ADAPTIVE_PROBE_URL", "https://discord.com/api/v10/gateway")

class NetAdaptiveOverlay(commands.Cog):
    """Lightweight RTT/error probe used to adapt Discord send throttle.

    This does NOT try to measure raw bandwidth (Mbps). It measures RTT + transient errors,
    which are the actionable signals for avoiding bursts and WAF/rate-limit escalation.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self._probe_loop.change_interval(seconds=PROBE_SECONDS)
        self._probe_loop.start()

    async def cog_unload(self) -> None:
        try:
            self._probe_loop.cancel()
        except Exception:
            pass
        try:
            if self._session is not None and not self._session.closed:
                await self._session.close()
        except Exception:
            pass

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=float(TIMEOUT_SECONDS))
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    @tasks.loop(seconds=30.0)
    async def _probe_loop(self) -> None:
        if not _al.ADAPTIVE_ENABLE:
            return

        # During Cloudflare cooldown, avoid hammering discord.com further.
        if _al.is_cloudflare_cooldown_active():
            return

        await self.bot.wait_until_ready()

        start = time.monotonic()
        try:
            sess = await self._ensure_session()
            async with sess.get(GATEWAY_URL) as resp:
                # Read small body to allow CF HTML detection if it ever happens.
                body = await resp.text()
                rtt_ms = (time.monotonic() - start) * 1000.0
                _al.set_rtt_ms(rtt_ms)

                if resp.status == 429:
                    # If this probe got rate-limited, treat as error.
                    if "cloudflare" in body.lower() or "error 1015" in body.lower() or "<!doctype html" in body.lower():
                        _al.record_cloudflare_1015("probe 429 html/cf")
                    else:
                        _al.record_error("probe_429")
                elif resp.status >= 500:
                    _al.record_error(f"probe_{resp.status}")

        except asyncio.TimeoutError:
            _al.record_error("probe_timeout")
            _al.set_rtt_ms(None)
        except Exception as e:
            # If the exception string contains CF signature, engage cooldown.
            s = str(e).lower()
            if "cloudflare" in s or "error 1015" in s or "<!doctype html" in s:
                _al.record_cloudflare_1015("probe_exc_cf")
            else:
                _al.record_error("probe_exc")
            _al.set_rtt_ms(None)

    @_probe_loop.before_loop
    async def _before_probe(self) -> None:
        await asyncio.sleep(1.0)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NetAdaptiveOverlay(bot))
