# -*- coding: utf-8 -*-
"""
Mirror newly-added Lucky cache entries ke thread, auto-create kalau belum ada.
Env yang dipakai:
- LPG_CACHE_MIRROR_ENABLE=1
- LPG_CACHE_PATH (default data/lpg_positive_cache.json)
- LPG_WL_THREAD_ID (opsional; jika tidak ada, akan dibuat)
- LPG_WL_PARENT_CHANNEL_ID (wajib untuk auto-create)
- LPG_WL_THREAD_NAME (default: "memory lucky")
"""
from __future__ import annotations
import os, json, asyncio, logging, time
import discord
from discord.ext import commands, tasks

log = logging.getLogger("nixe.cogs.a00_lpg_cache_mirror_overlay")

def _env(k, d=None):
    v = os.getenv(k)
    return v if (v is not None and str(v).strip() != "") else d

class LPGCacheMirror(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enable = _env("LPG_CACHE_MIRROR_ENABLE","1") == "1"
        self.path = _env("LPG_CACHE_PATH","data/lpg_positive_cache.json")
        # thread config
        self.thread_id = int(_env("LPG_WL_THREAD_ID","0") or "0")
        self.parent_id = int(_env("LPG_WL_PARENT_CHANNEL_ID","0") or "0")
        self.thread_name = _env("LPG_WL_THREAD_NAME","memory lucky")
        # polling
        self.min_interval = int(_env("LPG_CACHE_MIRROR_INTERVAL_SEC","5") or "5")
        self._last_len = 0

    async def _ensure_thread(self):
        if self.thread_id:
            return self.thread_id
        if not self.parent_id:
            return 0
        try:
            ch = self.bot.get_channel(self.parent_id) or await self.bot.fetch_channel(self.parent_id)
            if not ch:
                return 0
            # try find existing active threads by name
            try:
                for t in ch.threads:
                    if (t and getattr(t, "name", "").lower() == str(self.thread_name or "").lower()):
                        self.thread_id = int(t.id)
                        return self.thread_id
            except Exception:
                pass
            # create a new public thread
            t = await ch.create_thread(name=self.thread_name, auto_archive_duration=10080)  # 7 days
            self.thread_id = int(t.id)
            log.info("[lpg-cache-mirror] created thread '%s' under #%s -> id=%s",
                     self.thread_name, self.parent_id, self.thread_id)
            return self.thread_id
        except Exception as e:
            log.debug("[lpg-cache-mirror] ensure_thread err: %r", e)
            return 0

    async def _post(self, entry: dict):
        tid = await self._ensure_thread()
        if not tid:
            return
        try:
            th = self.bot.get_channel(tid) or await self.bot.fetch_channel(tid)
            if not th:
                return
            sha = entry.get("sha256","?")[:10]
            prov = entry.get("provider","?")
            sc = entry.get("score",1.0)
            ts = entry.get("ts", int(time.time()))
            txt = f"⭐ Cached Lucky — sha256={sha}… | src={prov} score={sc:.2f} ts={ts}"
            await th.send(txt)
            log.info("[lpg-cache-mirror] posted -> %s", tid)
        except Exception as e:
            log.debug("[lpg-cache-mirror] post failed: %r", e)

    async def _tick(self):
        try:
            if not self.enable: return
            data = []
            try:
                data = json.load(open(self.path, "r", encoding="utf-8"))
            except Exception:
                return
            n = len(data) if isinstance(data, list) else 0
            if n > self._last_len:
                for it in data[self._last_len:n]:
                    try:
                        if isinstance(it, dict) and it.get('ok', True) is False:
                            continue
                    except Exception:
                        pass
                    await self._post(it)
                self._last_len = n
        except Exception as e:
            log.debug("[lpg-cache-mirror] tick err: %r", e)

    @tasks.loop(seconds=5.0)
    async def _poll(self):
        await self._tick()

    @_poll.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(self.min_interval)
        try:
            data = json.load(open(self.path, "r", encoding="utf-8"))
            self._last_len = len(data) if isinstance(data, list) else 0
        except Exception:
            self._last_len = 0
        log.info("[lpg-cache-mirror] ready enable=%s parent=%s thread=%s name=%s start_len=%s",
                 self.enable, self.parent_id, self.thread_id, self.thread_name, self._last_len)

    def cog_unload(self):
        try:
            if self._poll.is_running():
                self._poll.cancel()
        except Exception:
            pass

    @commands.Cog.listener("on_ready")
    async def on_ready(self):
        if self.enable and not self._poll.is_running():
            self._poll.start()

async def setup(bot: commands.Bot):
    await bot.add_cog(LPGCacheMirror(bot))
