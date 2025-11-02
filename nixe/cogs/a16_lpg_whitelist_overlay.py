#!/usr/bin/env python3
# a16_lpg_whitelist_overlay.py
# - Shows Lucky Pull whitelist patterns in a single sticky message
# - Uses the same sticky thread (no new threads)
# - Safe at import; runs on on_ready

import os, asyncio, logging, json
from datetime import datetime, timezone
import discord
from discord.ext import commands

from nixe.helpers.lpg_thread_guard import ensure_sticky_thread, ensure_single_pinned

MARKER = "<nixe:lpg:whitelist:v1>"
REFRESH_SEC = int(os.getenv("LPG_WHITELIST_REFRESH_SEC", "900"))  # 15m

def _load_whitelist() -> list:
    # Sources: env JSON string or comma-separated list
    raw = os.getenv("LPG_WHITELIST_JSON", "").strip()
    if raw:
        try:
            arr = json.loads(raw)
            return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    csv = os.getenv("LPG_WHITELIST", "").strip()
    if csv:
        return [s.strip() for s in csv.split(",") if s.strip()]
    return []

def _render() -> str:
    items = _load_whitelist()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    head = f"**Lucky Pull Whitelist**  ·  `{ts}`\n"
    if not items:
        body = "- _Empty_. Add entries via `LPG_WHITELIST` (CSV) atau `LPG_WHITELIST_JSON`."
    else:
        body = "\n".join([f"- `{v}`" for v in items])
    note = "\n\n_Auto-updated. Pesan ini di-**edit**, bukan menambah pesan/thread baru._"
    return head + body + note

class LPGWhitelistOverlay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = None

    @commands.Cog.listener()
    async def on_ready(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._ensure_and_loop())

    async def _ensure_and_loop(self):
        th = await ensure_sticky_thread(self.bot)
        if not th:
            logging.error("[lpg-whitelist] Unable to get/create sticky thread")
            return
        msg = await ensure_single_pinned(th, MARKER)
        if not msg:
            logging.error("[lpg-whitelist] Unable to pin whitelist sticky message")
            return
        # initial
        try:
            await msg.edit(content=f"{MARKER}\n{_render()}")
        except Exception as e:
            logging.warning("[lpg-whitelist] initial edit failed: %s", e)
        # periodic refresh
        while True:
            try:
                await asyncio.sleep(REFRESH_SEC)
                await msg.edit(content=f"{MARKER}\n{_render()}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.warning("[lpg-whitelist] refresh failed: %s", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(LPGWhitelistOverlay(bot))
