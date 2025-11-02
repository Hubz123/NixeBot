#!/usr/bin/env python3
# a16_lpg_multikey_cache_overlay.py (v13b)
# Fix: no starred list-unpack inside list literal (Py syntax error). Now uses list() safely.
# Behavior:
#   - Uses single sticky thread via lpg_thread_guard
#   - Edits one pinned message in-place (no thread/message spam)
#   - No side effects at import; starts on_ready
#   - Cancels background task on cog unload

import os, asyncio, logging, re
from datetime import datetime, timezone
import discord
from discord.ext import commands

from nixe.helpers.lpg_thread_guard import ensure_sticky_thread, ensure_single_pinned

MARKER = "<nixe:lpg:cache:v1>"
UPDATE_SEC = int(os.getenv("LPG_CACHE_REFRESH_SEC", "900"))  # 15m default

def _mask_tail(s: str, n: int = 4) -> str:
    s = (s or "").strip()
    return ("*" * max(0, len(s) - n)) + s[-n:]

def _split_multi(val: str):
    if not val:
        return []
    return [p for p in re.split(r"[\s,;|]+", val.strip()) if p]

def _gather_gemini_keys():
    keys = []
    keys += _split_multi(os.getenv("GEMINI_API_KEY", ""))
    keys += _split_multi(os.getenv("GEMINI_API_KEY_B", ""))
    raw = os.getenv("GEMINI_KEYS", "").strip()
    if raw:
        if raw.startswith("["):
            try:
                import json as _json
                arr = _json.loads(raw)
                keys += [str(x).strip() for x in arr if str(x).strip()]
            except Exception:
                pass
        else:
            keys += [s.strip() for s in raw.split(",") if s.strip()]
    # dedupe while keeping order
    seen = set()
    uniq = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq

def _collect_state() -> str:
    keys = _gather_gemini_keys()
    models = os.getenv("GEMINI_MODELS", "gemini-2.5-flash-lite,gemini-2.5-flash")
    order = os.getenv("LPG_PROVIDER_ORDER", "gemini,groq")
    img_order = os.getenv("LPG_IMAGE_PROVIDER_ORDER", order)
    cool = os.getenv("GEMINI_COOLDOWN_SEC", "600")
    timeout = os.getenv("GEMINI_TIMEOUT_MS", "20000")
    retries = os.getenv("GEMINI_MAX_RETRIES", "2")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    masked = ", ".join([f"**{_mask_tail(k)}**" for k in keys]) if keys else "none"
    lines = [
        f"**Lucky Pull Cache Snapshot**  ·  `{ts}`",
        "",
        f"- Provider order: `{order}` (image: `{img_order}`)",
        f"- Models: `{models}`",
        f"- Keys: {len(keys)} -> {masked}",
        f"- Cooldown: `{cool}s` | Timeout: `{timeout}ms` | Retries: `{retries}`",
        "",
        "_Auto-updated. This message is edited in-place (no new messages/thread)._",
    ]
    return "\n".join(lines)

class LPGMultiKeyCacheOverlay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = None

    def cog_unload(self):
        try:
            if self._task and not self._task.done():
                self._task.cancel()
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._ensure_and_loop())

    async def _ensure_and_loop(self):
        th = await ensure_sticky_thread(self.bot)
        if not th:
            logging.error("[lpg-cache] Unable to get/create sticky thread")
            return
        msg = await ensure_single_pinned(th, MARKER)
        if not msg:
            logging.error("[lpg-cache] Unable to pin cache sticky message")
            return
        # initial populate
        try:
            await msg.edit(content=f"{MARKER}\n{_collect_state()}")
        except Exception as e:
            logging.warning("[lpg-cache] initial edit failed: %s", e)
        # periodic refresh (lightweight, just env snapshot)
        try:
            while True:
                await asyncio.sleep(UPDATE_SEC)
                await msg.edit(content=f"{MARKER}\n{_collect_state()}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.warning("[lpg-cache] refresh failed: %s", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(LPGMultiKeyCacheOverlay(bot))
