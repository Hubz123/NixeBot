"""
a18_health_restart_probe_overlay.py

Health / restart probe overlay for Nixe.

- All output goes to ONE thread under a configured base channel.
- Presence "online" info is kept in a single sticky embed message:
  - First run: create the sticky embed in the thread (optionally pinned).
  - Next restarts / hourly updates: EDIT the same embed (no new presence messages).
- Unexpected bot crash events (from main.py via "nixe_bot_crash" dispatch) are logged
  as separate text messages in the same thread, with cooldown to avoid spam.
"""


import logging
import datetime
import time
from typing import Optional

import os
import discord
from discord.ext import commands, tasks

def _env(key, default=None):
    return os.getenv(key, default)

def _as_bool(key, default=False):
    val = os.getenv(key)
    if val is None:
        return default
    v = val.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default

_log = logging.getLogger(__name__)



_STICKY_MARKER = "nixe-health-sticky"


class HealthRestartProbeOverlay(commands.Cog):
    """Send health / restart logs into a dedicated thread and sticky embed."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # Enabled flag (default: ON)
        self._enabled: bool = _as_bool("NIXE_HEALTH_ENABLE", True)

        # Base logging channel (text channel) — default to the ID requested.
        try:
            self._base_chan_id: int = int(_env("NIXE_HEALTH_BASE_CHAN_ID", "1431178130155896882") or "0")
        except ValueError:
            self._base_chan_id = 0

        # Thread name for all health logs
        self._thread_name: str = _env("NIXE_HEALTH_THREAD_NAME", "nixe-health-restart-log").strip() or "nixe-health-restart-log"

        # Internal state
        self._thread_id: int | None = None
        self._sticky_msg_id: int | None = None
        self._heartbeat_started: bool = False
        self._last_error_log_ts: float = 0.0  # for anti-spam on crash logs
        self._error_cooldown_sec: int = int(_env("NIXE_HEALTH_ERROR_COOLDOWN_SEC", "300") or "300")
        self._first_ready: bool = True

        _log.info(
            "[health-thread] enabled=%s base_chan_id=%s thread_name=%s",
            self._enabled,
            self._base_chan_id or "0",
            self._thread_name,
        )

    async def _ensure_thread(self) -> Optional[discord.Thread]:
        """Ensure we have a thread object for logging.

        - Reuse cached thread_id if still valid.
        - Otherwise, look for an existing active thread with matching name.
        - If not found, create a new public thread under the base channel.
        """
        if not self._enabled or not self._base_chan_id:
            return None

        # Check cached thread
        if self._thread_id:
            ch = self.bot.get_channel(self._thread_id)
            if isinstance(ch, discord.Thread):
                return ch

        base_ch = self.bot.get_channel(self._base_chan_id)
        if not isinstance(base_ch, discord.TextChannel):
            _log.warning("[health-thread] base channel %s not found or not TextChannel", self._base_chan_id)
            return None

        # Try to find an existing active thread with the configured name
        for th in base_ch.threads:
            if th.name == self._thread_name:
                self._thread_id = th.id
                return th

        # If not found, create a new public thread
        try:
            thread = await base_ch.create_thread(
                name=self._thread_name,
                type=discord.ChannelType.public_thread,
            )
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("[health-thread] failed to create thread in %s: %r", self._base_chan_id, exc)
            return None

        self._thread_id = thread.id
        _log.info("[health-thread] using thread id=%s name=%s", thread.id, thread.name)
        return thread

    async def _find_sticky_message(self, thread: discord.Thread) -> Optional[discord.Message]:
        """Look for an existing sticky embed message in the thread.

        We search recent messages authored by this bot with an embed footer marker.
        """
        me = self.bot.user
        if me is None:
            return None

        try:
            async for msg in thread.history(limit=50, oldest_first=False):
                if msg.author.id != me.id:
                    continue
                if not msg.embeds:
                    continue
                emb = msg.embeds[0]
                footer = emb.footer.text or ""
                if _STICKY_MARKER in footer:
                    return msg
        except Exception as exc:  # pragma: no cover - defensive
            _log.debug("[health-thread] failed to scan for sticky message: %r", exc)
        return None

    async def _ensure_sticky_message(self) -> Optional[discord.Message]:
        """Ensure we have the sticky presence embed message.

        - Try cached sticky_msg_id.
        - Else search for an embed with footer marker.
        - Else create a new embed message and (optionally) pin it.
        """
        if not self._enabled:
            return None

        thread = await self._ensure_thread()
        if thread is None:
            return None

        # Try cached id
        if self._sticky_msg_id:
            try:
                msg = await thread.fetch_message(self._sticky_msg_id)
                return msg
            except Exception:
                # cache invalid; fall through to search
                self._sticky_msg_id = None

        # Try to find existing sticky message
        msg = await self._find_sticky_message(thread)
        if msg is not None:
            self._sticky_msg_id = msg.id
            return msg

        # Create new sticky embed
        now = datetime.datetime.now(datetime.timezone.utc)
        emb = self._build_presence_embed(now)
        try:
            msg = await thread.send(embed=emb)
            self._sticky_msg_id = msg.id
            # Try pin; ignore failures
            try:
                await msg.pin()
            except Exception:
                pass
            return msg
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("[health-thread] failed to create sticky message: %r", exc)
            return None

    def _build_presence_embed(self, now: datetime.datetime) -> discord.Embed:
        """Build the presence sticky embed."""
        ts = now.isoformat(timespec="seconds")
        emb = discord.Embed(
            title="Nixe Presence",
            description="Status presence / online NixeBot.",
            colour=discord.Colour.green(),
            timestamp=now,
        )
        emb.add_field(name="Status", value="Online", inline=True)
        emb.add_field(name="Last update", value=ts, inline=True)
        emb.set_footer(text=_STICKY_MARKER)
        return emb

    async def _update_presence_sticky(self) -> None:
        """Create or update the sticky presence embed."""
        if not self._enabled:
            return
        thread = await self._ensure_thread()
        if thread is None:
            return

        msg = await self._ensure_sticky_message()
        if msg is None:
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        emb = self._build_presence_embed(now)
        try:
            await msg.edit(embed=emb)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("[health-thread] failed to edit sticky presence message: %r", exc)

    async def _send_error_line(self, content: str) -> None:
        """Send a separate error log line into the thread (for crashes)."""
        if not self._enabled:
            return
        thread = await self._ensure_thread()
        if thread is None:
            return
        try:
            await thread.send(content)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("[health-thread] failed to send error message: %r", exc)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """On first ready per process, update the sticky presence embed.

        We do NOT send new messages for presence each restart. Presence information
        is always stored in a single sticky embed, which is edited.
        """
        if not self._enabled:
            return

        if self._first_ready:
            await self._update_presence_sticky()
            self._first_ready = False

        # Start heartbeat loop once
        if not self._heartbeat_started:
            self._heartbeat_started = True
            self.heartbeat_loop.start()

    @tasks.loop(hours=1.0)
    async def heartbeat_loop(self) -> None:
        """Hourly heartbeat: update sticky embed to confirm bot is still alive."""
        if not self._enabled:
            return
        await self._update_presence_sticky()

    @heartbeat_loop.before_loop
    async def _before_heartbeat(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener("on_nixe_bot_crash")
    async def on_nixe_bot_crash(self, exc: Exception) -> None:
        """Log unexpected crash of the Discord client into the health thread.

        These logs are separate from the sticky presence embed, and are throttled
        with a cooldown to avoid spam.
        """
        if not self._enabled:
            return

        now = time.time()
        if now - self._last_error_log_ts < float(self._error_cooldown_sec):
            # Anti-spam: only log crash once per cooldown window
            return
        self._last_error_log_ts = now

        ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        summary = repr(exc)
        if len(summary) > 300:
            summary = summary[:297] + "..."

        await self._send_error_line(f"[health] bot crash detected at {ts}: {summary}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HealthRestartProbeOverlay(bot))
