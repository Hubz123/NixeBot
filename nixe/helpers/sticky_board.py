
#!/usr/bin/env python3
"""StickyBoard helper
Keeps exactly ONE pinned message inside a target thread. The message is edited
in-place to show latest content (no thread spam, no new messages).
"""
import os, asyncio, logging
from typing import List, Optional
import discord

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
        try:
            ch = await self.bot.fetch_channel(tid)
        except Exception as e:
            logging.error("[StickyBoard] fetch_channel(%s) failed: %s", tid, e)
            return None
        if isinstance(ch, discord.Thread):
            if ch.archived:
                try:
                    await ch.edit(archived=False, reason="StickyBoard revive")
                except Exception:
                    pass
            return ch
        # Fallback: if a TextChannel is provided, create a thread for the board
        if isinstance(ch, discord.TextChannel):
            try:
                th = await ch.create_thread(name=self.title[:80] or "sticky-board",
                                            auto_archive_duration=10080,
                                            reason="StickyBoard fallback thread creation")
                return th
            except Exception as e:
                logging.error("[StickyBoard] create_thread fallback failed: %s", e)
        else:
            logging.warning("[StickyBoard] unsupported parent type: %s", type(ch).__name__)
        return None

    async def _find_or_create_sticky(self) -> Optional[discord.Message]:
        th = await self._get_thread()
        if not th:
            return None
        # Find pinned with marker
        try:
            pins = await th.pins()
        except Exception as e:
            logging.warning("[StickyBoard] pins() failed: %s", e)
            pins = []
        for m in pins:
            if self.marker in (m.content or ""):
                self._msg = m
                return m
        # Create & pin
        try:
            msg = await th.send(f"{self.marker}\n**{self.title}**\n(auto-updated)")
            try:
                await msg.pin()
            except Exception:
                pass
            self._msg = msg
            return msg
        except Exception as e:
            logging.error("[StickyBoard] send/pin failed: %s", e)
            return None

    async def update_lines(self, lines: List[str], footer: Optional[str] = None):
        m = self._msg or await self._find_or_create_sticky()
        if not m:
            return
        desc = "\n".join(lines[:200])  # soft cap
        embed = discord.Embed(title=self.title, description=desc)
        if footer:
            embed.set_footer(text=footer)
        try:
            await m.edit(content=self.marker, embed=embed)
        except Exception as e:
            logging.warning("[StickyBoard] edit failed: %s", e)
