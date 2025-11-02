import asyncio, logging, os
from typing import List, Optional, Tuple
import discord


StickyBoard: keeps a SINGLE message (sticky) inside a specified thread channel.
It edits the embed repeatedly instead of posting new messages.
ENV:
  - LPG_CACHE_THREAD_ID: channel/thread id for lucky-pull cache board
  - LPG_WHITELIST_THREAD_ID: channel/thread id for whitelist board (optional; fallback to LPG_CACHE_THREAD_ID)
  - STICKY_MARKER: a unique marker in message content to find the sticky message (default: "[LPG-STICKY]")

class StickyBoard:
    def __init__(self, bot: discord.Client, thread_id_env: str, marker: str, title: str):
        self.bot = bot
        self.thread_id_env = thread_id_env
        self.marker = marker
        self.title = title
        self._msg: Optional[discord.Message] = None

    async def _get_thread(self) -> Optional[discord.Thread]:
        tid = int(os.getenv(self.thread_id_env, "0") or "0")
        if not tid:
            logging.warning("[StickyBoard] %s not set", self.thread_id_env)
            return None
        ch = self.bot.get_channel(tid)
        if isinstance(ch, discord.Thread):
            return ch
        # Try fetch in case it's not cached
        try:
            ch = await self.bot.fetch_channel(tid)
            if isinstance(ch, discord.Thread):
                return ch
        except Exception as e:
            logging.warning("[StickyBoard] fetch_channel failed: %s", e)
        return None

    async def _find_or_create_sticky(self) -> Optional[discord.Message]:
        thread = await self._get_thread()
        if not thread:
            return None
        # Try find existing bot message with marker
        try:
            async for m in thread.history(limit=50, oldest_first=False):
                if m.author.id == self.bot.user.id and self.marker in (m.content or ""):
                    self._msg = m
                    return m
        except Exception as e:
            logging.warning("[StickyBoard] history failed: %s", e)
        # Create new if none
        try:
            embed = discord.Embed(title=self.title, description="(initializing...)")
            m = await thread.send(content=self.marker, embed=embed)
            try:
                await m.pin()
            except Exception:
                pass
            self._msg = m
            return m
        except Exception as e:
            logging.warning("[StickyBoard] create failed: %s", e)
            return None

    async def update_lines(self, lines: List[str], footer: Optional[str]=None):
        m = self._msg or await self._find_or_create_sticky()
        if not m:
            return
        desc = "\n".join(lines[:4000//20])  # rough limit safeguard
        embed = discord.Embed(title=self.title, description=desc)
        if footer:
            embed.set_footer(text=footer)
        try:
            await m.edit(content=self.marker, embed=embed)
        except Exception as e:
            logging.warning("[StickyBoard] edit failed: %s", e)
