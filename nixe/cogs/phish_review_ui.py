# -*- coding: utf-8 -*-
"""
phish_review_ui

RAGU phishing review UI: BAN / FALSE buttons posted to a dedicated review thread
under the same parent channel as LOG_CHANNEL_ID / PHISH_LOG_CHAN_ID.

No reliance on the YouTube watchlist thread.
"""
from __future__ import annotations

import os
import logging
import contextlib
from typing import Optional
import discord
from discord.ext import commands
from nixe.helpers import phish_review_memory as _mem

log = logging.getLogger(__name__)


def _env_int(name: str, default: int = 0) -> int:
    try:
        v = (os.getenv(name) or "").strip()
        return int(v) if v else default
    except Exception:
        return default


# Parent channel where the review thread lives.
# Prefer LOG_CHANNEL_ID (often enforced); fall back to PHISH_LOG_CHAN_ID.
PARENT_CHANNEL_ID = _env_int("LOG_CHANNEL_ID", 0) or _env_int("PHISH_LOG_CHAN_ID", 0)

# Optional hard override to post directly into an existing thread/channel ID.
REVIEW_THREAD_ID_OVERRIDE = _env_int("PHISH_REVIEW_THREAD_ID", 0)

# Thread name to find/create under the parent channel.
REVIEW_THREAD_NAME = (os.getenv("PHISH_REVIEW_THREAD_NAME", "Phish Review (FP)") or "").strip() or "Phish Review (FP)"


async def resolve_review_thread(
    bot: discord.Client,
    guild: discord.Guild,
) -> Optional[discord.abc.Messageable]:
    """
    Resolve destination for review messages:
    1) PHISH_REVIEW_THREAD_ID override if set
    2) A thread named REVIEW_THREAD_NAME under PARENT_CHANNEL_ID (create if missing)
    3) Fallback: parent channel itself (if resolvable)
    """
    # 1) Override ID
    if REVIEW_THREAD_ID_OVERRIDE:
        ch = guild.get_channel(REVIEW_THREAD_ID_OVERRIDE)
        if not ch:
            try:
                ch = await guild.fetch_channel(REVIEW_THREAD_ID_OVERRIDE)
            except Exception:
                ch = None
        if ch:
            return ch

    # 2) Parent channel by ID
    parent = None
    if PARENT_CHANNEL_ID:
        parent = guild.get_channel(PARENT_CHANNEL_ID)
        if not parent:
            try:
                parent = await guild.fetch_channel(PARENT_CHANNEL_ID)
            except Exception:
                parent = None

    # If parent resolves to a Thread, redirect to its parent channel
    if isinstance(parent, discord.Thread):
        parent = parent.parent

    if isinstance(parent, discord.TextChannel):
        # Try active threads first
        try:
            for th in list(getattr(parent, "threads", []) or []):
                if th and th.name == REVIEW_THREAD_NAME:
                    return th
        except Exception:
            pass

        # Try recent archived threads (best-effort)
        try:
            async for th in parent.archived_threads(limit=50):
                if th and th.name == REVIEW_THREAD_NAME:
                    return th
        except Exception:
            pass

        # Create thread if possible
        try:
            th = await parent.create_thread(
                name=REVIEW_THREAD_NAME,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=10080,  # 7 days
                reason="Nixe: create phishing review thread",
            )
            return th
        except Exception as e:
            log.warning("[phish-review] cannot create review thread under parent=%s: %r", getattr(parent, "id", None), e)
            return parent

    # 3) Fallback
    return parent


"""Compatibility notes

This cog uses discord.py 2.x UI components (discord.ui.View/buttons).
If discord.py < 2.0 is present (no discord.ui.View), we provide a minimal
shim so the module can still be imported without crashing. In that case,
interactive buttons will be unavailable.
"""

try:
    _ui = discord.ui
    _HAS_UI = hasattr(_ui, "View")
except Exception:
    _ui = None
    _HAS_UI = False


if _HAS_UI:
    _BaseView = _ui.View
    _button = _ui.button
    _UIButton = _ui.Button
else:
    class _BaseView:  # type: ignore
        def __init__(self, *args, **kwargs):
            return

    def _button(*args, **kwargs):  # type: ignore
        def deco(fn):
            return fn
        return deco

    class _UIButton:  # type: ignore
        pass


class PhishReviewView(_BaseView):
    def __init__(
        self,
        *,
        signature: str | None = None,
        sig: str | None = None,
        target_user_id: int = 0,
        guild_id: int = 0,
        delete_days: int = 0,
        evidence: dict | None = None,
        reason_prefix: str | None = None,
    ):
        # NOTE: keep __init__ kwargs backward-compatible with older patches.
        super().__init__(timeout=None)  # type: ignore
        self.signature = (signature or sig or "").strip()
        self.target_user_id = int(target_user_id or 0)
        self.guild_id = int(guild_id or 0)
        self.delete_days = max(0, min(7, int(delete_days or 0)))
        self.evidence = evidence or {}
        self.reason_prefix = (reason_prefix or "").strip()

    def _can_act(self, interaction: discord.Interaction) -> bool:
        try:
            m = interaction.user
            if not isinstance(m, (discord.Member, discord.User)):
                return False
            if isinstance(m, discord.Member):
                perms = m.guild_permissions
                return bool(perms.ban_members or perms.manage_guild or perms.administrator)
            return False
        except Exception:
            return False

    async def _ack(self, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.response.send_message(content, ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send(content, ephemeral=True)
            except Exception:
                pass

    @_button(label="BAN", style=getattr(discord.ButtonStyle, "danger", 4))
    async def ban_btn(self, interaction: "discord.Interaction", button: _UIButton):
        if not self._can_act(interaction):
            return await self._ack(interaction, "No permission.")
        guild = interaction.guild
        if not guild:
            return await self._ack(interaction, "Guild not found.")
        try:
            user = guild.get_member(self.target_user_id) or await guild.fetch_member(self.target_user_id)
        except Exception:
            user = None
        try:
            if user:
                await guild.ban(user, delete_message_days=self.delete_days, reason="RAGU phishing review -> BAN")
            else:
                # fallback: ban by user object if fetch failed
                await guild.ban(discord.Object(id=self.target_user_id), delete_message_days=self.delete_days, reason="RAGU phishing review -> BAN")
        except Exception as e:
            return await self._ack(interaction, f"Ban failed: {e!r}")

        # Persist signature as confirmed attack
        with contextlib.suppress(Exception):
            _mem.mark_banned(self.signature)
        try:
            await interaction.message.edit(content=interaction.message.content + "\n✅ Action: BAN", view=None)
        except Exception:
            pass
        await self._ack(interaction, "Banned. Signature saved.")

    @_button(label="FALSE", style=getattr(discord.ButtonStyle, "secondary", 2))
    async def false_btn(self, interaction: "discord.Interaction", button: _UIButton):
        if not self._can_act(interaction):
            return await self._ack(interaction, "No permission.")
        # Persist signature as false positive
        with contextlib.suppress(Exception):
            _mem.mark_false(self.signature)
        try:
            await interaction.message.edit(content=interaction.message.content + "\n✅ Action: FALSE POSITIVE", view=None)
        except Exception:
            pass
        await self._ack(interaction, "Marked FALSE. Signature will be skipped next time.")
# --- Compatibility shim: make this module a valid extension/cog file ---
# The actual UI helpers (resolve_review_thread, PhishReviewView) are used by other cogs.
# This no-op Cog + setup(bot) keeps loaders/smoke tests happy without changing runtime behavior.

class PhishReviewUICog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

async def setup(bot: commands.Bot):
    await bot.add_cog(PhishReviewUICog(bot))
