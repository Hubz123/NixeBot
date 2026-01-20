# -*- coding: utf-8 -*-
"""a17_1_lpg_denylist_thread_manager_overlay

Creates and manages the LPG denylist thread under the *exact* LPG parent channel.

Hard requirement:
- The denylist MUST live under parent channel id 1431178130155896882.
- No fallback to other parents/threads.

Behavior:
- On boot, create (or find by name) a denylist thread in that parent.
- Load deny entries from the thread into in-process sets.
- Provide a lightweight enqueue API for other cogs to persist new deny entries.

Message format written to denylist thread:
  deny sha1=<40hex> ahash=<16hex> src=<token>
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional, Tuple

import discord
from discord.ext import commands

from nixe.helpers import lpg_denylist


log = logging.getLogger(__name__)


PARENT_CHANNEL_ID_REQUIRED = 1431178130155896882

DEFAULT_THREAD_NAME = "Denylist LPG (Unlearn)"

_DENY_RE = re.compile(r"sha1=([0-9a-f]{40})\s+ahash=([0-9a-f]{16})", re.I)


def _env_int(k: str) -> int:
    try:
        return int(str(os.getenv(k, "")).strip() or "0")
    except Exception:
        return 0


def _env_str(k: str, d: str) -> str:
    v = os.getenv(k)
    return str(v) if v is not None else d


class LPGDenylistThreadManager(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.parent_id = _env_int("LPG_PARENT_CHANNEL_ID")
        self.thread_name = _env_str("LPG_DENYLIST_THREAD_NAME", DEFAULT_THREAD_NAME)
        self.scan_limit = max(200, _env_int("LPG_DENYLIST_BOOT_SCAN_LIMIT") or 4000)
        self._thread: Optional[discord.Thread] = None
        self._q: "asyncio.Queue[Tuple[str, str, str]]" = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

    @commands.Cog.listener()
    async def on_ready(self):
        # Enforce exact required parent id. No fallback.
        if not self.parent_id:
            # Try to read from runtime_env overlay if it exported env later.
            self.parent_id = _env_int("LPG_PARENT_CHANNEL_ID")
        if not self.parent_id:
            log.error("[lpg-deny] missing LPG_PARENT_CHANNEL_ID; required=%s", PARENT_CHANNEL_ID_REQUIRED)
            return
        if int(self.parent_id) != int(PARENT_CHANNEL_ID_REQUIRED):
            log.error(
                "[lpg-deny] LPG_PARENT_CHANNEL_ID=%s does not match required=%s; refusing fallback and forcing required.",
                self.parent_id,
                PARENT_CHANNEL_ID_REQUIRED,
            )
            # Force to required id to satisfy the hard requirement.
            self.parent_id = int(PARENT_CHANNEL_ID_REQUIRED)

        # Bind/create thread and load denylist.
        await self._ensure_thread()
        await self._load_from_thread()

        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="lpg-denylist-writer")

    async def _ensure_thread(self) -> Optional[discord.Thread]:
        # Fetch parent channel (must be TextChannel-like).
        parent = self.bot.get_channel(int(self.parent_id))
        if parent is None:
            try:
                parent = await self.bot.fetch_channel(int(self.parent_id))
            except Exception as e:
                log.error("[lpg-deny] cannot fetch parent channel %s: %r", self.parent_id, e)
                return None

        if not isinstance(parent, (discord.TextChannel, discord.ForumChannel, discord.Thread)):
            log.error("[lpg-deny] parent channel type unsupported: %s", type(parent).__name__)
            return None

        # If a thread with matching name exists and is accessible, reuse it.
        try:
            # Active threads
            if hasattr(parent, "threads"):
                for th in list(getattr(parent, "threads") or []):
                    try:
                        if isinstance(th, discord.Thread) and (th.name or "").strip() == self.thread_name:
                            self._thread = th
                            log.warning("[lpg-deny] found existing thread name=%s id=%s", th.name, th.id)
                            return th
                    except Exception:
                        continue
        except Exception:
            pass

        # Try to discover archived public threads (best effort)
        try:
            if isinstance(parent, discord.TextChannel):
                async for th in parent.archived_threads(limit=100):
                    try:
                        if (th.name or "").strip() == self.thread_name:
                            self._thread = th
                            log.warning("[lpg-deny] found archived thread name=%s id=%s", th.name, th.id)
                            return th
                    except Exception:
                        continue
        except Exception:
            pass

        # Create a new thread in this parent.
        try:
            if isinstance(parent, discord.TextChannel):
                starter = await parent.send("[lpg-deny] denylist thread (auto). Do not delete.")
                th = await starter.create_thread(name=self.thread_name, auto_archive_duration=10080)
                self._thread = th
                log.warning("[lpg-deny] created thread name=%s id=%s parent=%s", th.name, th.id, parent.id)
                return th
            elif isinstance(parent, discord.ForumChannel):
                # Forum: create post/thread
                th = await parent.create_thread(name=self.thread_name, content="[lpg-deny] denylist thread (auto). Do not delete.")
                # discord.py returns ThreadWithMessage; normalize
                thread_obj = getattr(th, "thread", None) or th
                if isinstance(thread_obj, discord.Thread):
                    self._thread = thread_obj
                    log.warning("[lpg-deny] created forum thread name=%s id=%s parent=%s", thread_obj.name, thread_obj.id, parent.id)
                    return thread_obj
        except Exception as e:
            log.error("[lpg-deny] create thread failed: %r", e)
            return None

        return None

    async def _load_from_thread(self) -> None:
        th = self._thread
        if not th:
            return
        pairs = []
        scanned = 0
        try:
            async for msg in th.history(limit=int(self.scan_limit), oldest_first=False):
                scanned += 1
                txt = (getattr(msg, "content", "") or "")
                m = _DENY_RE.search(txt)
                if not m:
                    continue
                sha1 = m.group(1).lower()
                ah = m.group(2).lower()
                pairs.append((sha1, ah))
        except Exception as e:
            log.error("[lpg-deny] load history failed: %r", e)
        if pairs:
            lpg_denylist.add_many(pairs)
        st = lpg_denylist.stats()
        log.warning("[lpg-deny] loaded scanned=%s denied_sha1=%s denied_ahash=%s thread=%s", scanned, st["sha1"], st["ahash"], th.id)

    async def enqueue_deny(self, sha1: str, ahash: str, src: str = "unlearn") -> None:
        """Public API called by other cogs: add deny and persist to thread."""
        s = (sha1 or "").strip().lower()
        a = (ahash or "").strip().lower()
        if not s or len(s) != 40:
            return
        if not a or len(a) != 16:
            a = "0" * 16
        # Update in-process immediately
        lpg_denylist.add(s, a)
        try:
            await self._q.put((s, a, (src or "unlearn")[:24]))
        except Exception:
            return

    async def _worker(self) -> None:
        # Single writer loop; best-effort.
        while True:
            try:
                sha1, ah, src = await self._q.get()
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(0.25)
                continue

            try:
                if not self._thread:
                    await self._ensure_thread()
                th = self._thread
                if not th:
                    continue
                await th.send(f"deny sha1={sha1} ahash={ah} src={src}")
            except Exception as e:
                log.warning("[lpg-deny] persist failed sha1=%s: %r", sha1[:8], e)
            finally:
                try:
                    self._q.task_done()
                except Exception:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(LPGDenylistThreadManager(bot))


