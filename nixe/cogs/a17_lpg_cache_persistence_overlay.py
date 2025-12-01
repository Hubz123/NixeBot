
# -*- coding: utf-8 -*-
"""
a17_lpg_cache_persistence_overlay  (bind-safe)
---------------------------------------------
- Auto-create ONE cache thread under parent channel, then re-bind on restart.
- Searches existing thread by name in ACTIVE and ARCHIVED lists before creating.
- On boot, rebuilds memory cache by reading JSON lines from that thread.
"""
from __future__ import annotations
import os, json, asyncio, logging
from typing import Optional
import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

def _env(k: str, d: str = "") -> str:
    v = os.getenv(k)
    return str(v) if v is not None else d

THREAD_NAME = os.getenv("LPG_CACHE_THREAD_NAME", "LPG Cache (mem)")

class LPGCachePersistence(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.parent_id = int(_env("LPG_CACHE_THREAD_ID", _env("LOG_CHANNEL_ID","0")) or 0)
        self.max_entries = int(_env("LPG_CACHE_MAX_ENTRIES","1000") or "1000")
        self.max_boot_lines = int(_env("LPG_CACHE_BOOT_LINES","500") or "500")
        self.thread: Optional[discord.Thread] = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self._ensure_lock = asyncio.Lock()
        self._writer.start()

    # ---------- writer loop ----------
    @tasks.loop(seconds=1.5)
    async def _writer(self):
        if self.thread is None:
            return
        try:
            chunk = []
            while not self.queue.empty() and len(chunk) < 10:
                chunk.append(await self.queue.get())
            if not chunk:
                return
            # Previously this wrote JSON payloads into the cache thread:
            # payload = "```json\n" + "\n".join(json.dumps(x, ensure_ascii=False) for x in chunk) + "\n```"
            # await self.thread.send(payload)
            # Now we only log a short message and drop the entries to keep Discord logs clean.
            log.debug("[lpg-cache] writer flushed %s entries (no Discord JSON)", len(chunk))
        except Exception as e:
            log.warning("[lpg-cache] writer failed: %r", e)

    @_writer.before_loop
    async def _before_writer(self):
        await self.bot.wait_until_ready()

    # ---------- bind/find/create thread ----------
    async def _find_existing_thread(self, ch: discord.TextChannel) -> Optional[discord.Thread]:
        # 1) active threads in channel cache
        try:
            for th in getattr(ch, "threads", []):
                if isinstance(th, discord.Thread) and th.name == THREAD_NAME:
                    return th
        except Exception:
            pass

        # 2) archived threads (best-effort; different discord.py versions expose different APIs)
        # Try a few methods defensively.
        # 2a) public_archived_threads iterator
        try:
            itr = ch.archived_threads(limit=50)  # some versions expose this
            async for th in itr:
                if isinstance(th, discord.Thread) and th.name == THREAD_NAME:
                    return th
        except Exception:
            pass
        # 2b) fetch_archived_threads (old style)
        for meth in ("fetch_archived_threads", "public_archived_threads", "fetch_public_archived_threads"):
            try:
                res = await getattr(ch, meth)(limit=50)  # type: ignore
                threads = getattr(res, "threads", res)
                for th in threads:
                    try:
                        if isinstance(th, discord.Thread) and th.name == THREAD_NAME:
                            return th
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    async def _ensure_thread(self):
        if self.thread is not None:
            return
        if not self.parent_id:
            log.warning("[lpg-cache] parent channel id missing")
            return
        async with self._ensure_lock:
            if self.thread is not None:
                return
            try:
                ch = self.bot.get_channel(self.parent_id) or await self.bot.fetch_channel(self.parent_id)
                ex = await self._find_existing_thread(ch)  # type: ignore
                if ex is not None:
                    self.thread = ex
                    log.warning("[lpg-cache] bind existing thread: #%s (%s)", self.thread.name, self.thread.id)
                else:
                    self.thread = await ch.create_thread(name=THREAD_NAME, auto_archive_duration=10080)  # 7 days
                    log.warning("[lpg-cache] created thread: #%s (%s)", self.thread.name, self.thread.id)
            except Exception as e:
                log.warning("[lpg-cache] ensure thread failed: %r", e)

    # ---------- lifecycle ----------
    @commands.Cog.listener()
    async def on_ready(self):
        # configure memory size
        try:
            from nixe.helpers import lpg_cache_memory as cache
            cache.configure(self.max_entries)
        except Exception:
            pass
        await self._ensure_thread()
        await self._bootstrap_load()

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        if self.thread and thread.id == self.thread.id:
            log.warning("[lpg-cache] bound thread deleted; will rebind on next ensure.")
            self.thread = None

    async def _bootstrap_load(self):
        if not self.thread:
            return
        try:
            from nixe.helpers import lpg_cache_memory as cache
            lines = 0
            async for msg in self.thread.history(limit=self.max_boot_lines):
                if not msg.content or not msg.content.startswith("```json"):
                    continue
                raw = msg.content.strip().strip("```").replace("json","",1).strip()
                for line in raw.splitlines():
                    line = line.strip()
                    if not line or line in ("{","}"):
                        continue
                    try:
                        j = json.loads(line)
                        # reconstruct minimal entry
                        cache_entry = {
                            "sha1": j.get("sha1",""),
                            "ahash": j.get("ahash","0"*16),
                            "ok": bool(j.get("ok", False)),
                            "score": float(j.get("score",0.0)),
                            "via": str(j.get("via","cache:disk")),
                            "reason": str(j.get("reason","")),
                            "w": int(j.get("w",0)),
                            "h": int(j.get("h",0)),
                            "ts": float(j.get("ts",0)),
                        }
                        from nixe.helpers.lpg_cache_memory import _CACHE, _INDEX_AHASH
                        if cache_entry["sha1"]:
                            _CACHE[cache_entry["sha1"]] = cache_entry
                            _INDEX_AHASH.setdefault(cache_entry["ahash"], []).append(cache_entry["sha1"])
                            lines += 1
                    except Exception:
                        continue
            log.warning("[lpg-cache] bootstrap loaded %s entries", lines)
        except Exception as e:
            log.warning("[lpg-cache] bootstrap failed: %r", e)

    # public method used by hook overlay
    async def persist(self, entry: dict):
        if self.thread is None:
            return
        await self.queue.put(entry)

async def setup(bot: commands.Bot):
    await bot.add_cog(LPGCachePersistence(bot))
