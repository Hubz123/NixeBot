# nixe/cogs/a00a_whitelist_thread_singleton_overlay.py
# Collapse duplicate "Whitelist LPG (FP)" threads; reuse one and archive duplicates.
import os
import asyncio
import logging
import discord
from discord.ext import commands

_log = logging.getLogger(__name__)


def _getenv(k: str, d: str = "") -> str:
    return os.getenv(k, d)


class WhitelistThreadSingleton(commands.Cog):
    """Ensure there is a single 'Whitelist LPG (FP)' thread and archive duplicates.

    This cog periodically scans the configured log channel for threads named
    LPG_WHITELIST_THREAD_NAME (default: 'Whitelist LPG (FP)'). One thread is
    chosen as the canonical whitelist thread; any other threads with the same
    name in that channel are archived. For observability, we log which thread
    is chosen, but only when it changes to avoid log spam.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Prefer LOG_CHANNEL_ID, fallback to NIXE_PHISH_LOG_CHAN_ID (same as previous overlay).
        self.log_chan_id: int = int(
            _getenv("LOG_CHANNEL_ID") or _getenv("NIXE_PHISH_LOG_CHAN_ID") or "0"
        )
        # Name of the whitelist thread to keep.
        self.thread_name: str = _getenv("LPG_WHITELIST_THREAD_NAME", "Whitelist LPG (FP)")
        # Background task handle.
        self._task: asyncio.Task | None = None
        # Last chosen thread id + archived flag (for de-duplicating logs).
        self._last_chosen_id: int | None = None
        self._last_archived_flag: int | None = None

    async def _ensure_once(self) -> None:
        """Run a single reconciliation pass.

        - Finds the log channel in any guild the bot is in.
        - Scans its active threads for ones matching thread_name.
        - Picks one canonical thread (prefer unarchived).
        - Archives any other threads with the same name.
        - Emits an INFO log only when the chosen thread or archived status changes.
        """
        if not self.log_chan_id:
            return

        # Wait until the bot is ready and guild list is populated.
        await self.bot.wait_until_ready()

        guilds = list(getattr(self.bot, "guilds", []) or [])
        if not guilds:
            return

        chan: discord.abc.GuildChannel | None = None
        for g in guilds:
            try:
                c = g.get_channel(self.log_chan_id)  # type: ignore[arg-type]
            except Exception:
                continue
            if c is not None:
                chan = c
                break

        if chan is None:
            return

        # Only text-like channels can have text threads attached.
        if not isinstance(chan, (discord.TextChannel, discord.ForumChannel)):
            return

        # Collect candidate threads matching the configured name.
        candidates: list[discord.Thread] = []
        try:
            for t in getattr(chan, "threads", []):
                if isinstance(t, discord.Thread) and (t.name or "") == self.thread_name:
                    candidates.append(t)
        except Exception:
            # If we cannot enumerate threads, bail out quietly.
            return

        if not candidates:
            # No whitelist thread yet; nothing to collapse. Avoid noisy logs.
            return

        # Prefer an unarchived thread; otherwise just take the first.
        chosen: discord.Thread | None = None
        for t in candidates:
            if not getattr(t, "archived", False):
                chosen = t
                break
        if chosen is None:
            chosen = candidates[0]

        # Archive any duplicates (same name) that are not the chosen thread.
        for t in candidates:
            if t.id == chosen.id:
                continue
            if not getattr(t, "archived", False):
                try:
                    await t.edit(archived=True, locked=True, reason="Whitelist LPG singleton collapse")
                except Exception:
                    # Do not crash if we cannot archive a duplicate thread.
                    pass

        archived_flag = 1 if getattr(chosen, "archived", False) else 0

        # Only log when the chosen thread or its archived flag changes.
        if (
            chosen.id != (self._last_chosen_id or 0)
            or archived_flag != (self._last_archived_flag if self._last_archived_flag is not None else -1)
        ):
            _log.info(
                "[lpg-wl-singleton] chosen=%s archived=%s name=%r",
                chosen.id,
                archived_flag,
                self.thread_name,
            )
            self._last_chosen_id = chosen.id
            self._last_archived_flag = archived_flag

    async def _run_periodic(self) -> None:
        """Background loop that periodically calls _ensure_once."""
        # Initial pass shortly after startup.
        try:
            await self._ensure_once()
        except Exception:
            # Never let the background task die due to an exception.
            pass

        # Interval kept at 300s (5 minutes), matching previous behavior.
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(300)
                await self._ensure_once()
            except Exception:
                # Swallow exceptions to keep the task alive.
                continue

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Start the background reconciliation task once.
        if self._task is None:
            self._task = asyncio.create_task(self._run_periodic())


async def setup(bot: commands.Bot) -> None:
    add = getattr(bot, "add_cog")
    res = add(WhitelistThreadSingleton(bot))
    import inspect

    if inspect.isawaitable(res):
        await res
