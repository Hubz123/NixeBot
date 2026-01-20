# -*- coding: utf-8 -*-
"""nixe.cogs.a00_first_touchdown_overlay

First Touchdown overlay:
- Enforces FIRST_TOUCHDOWN_* env policy on phishing events and (if present) pHash match events.

Safety hardening (Render free plan focus):
- Reduce false-positive bans when the only signal is an image-hash hit on a *single* valid WEBP attachment.
- Fast-path ban for high-confidence phishing blast pattern: @everyone + 4+ image attachments where
  filenames look like PNG but the attachment metadata indicates WEBP (".png berkedok webp").

No config changes; purely reads runtime_env.json/.env.
"""

from __future__ import annotations

import os
import logging
import contextlib
import re

import discord
from discord.ext import commands

log = logging.getLogger("nixe.cogs.a00_first_touchdown_overlay")

_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}]+", re.IGNORECASE)
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _ids(val: str) -> set[int]:
    out: set[int] = set()
    for tok in (val or "").replace(";", ",").split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.add(int(tok))
    return out


def _ext(name: str) -> str:
    n = (name or "").strip().lower()
    if "." not in n:
        return ""
    return n.rsplit(".", 1)[-1]


def _is_image_attachment(att: discord.Attachment) -> bool:
    try:
        fn = (att.filename or "").lower().strip()
        if any(fn.endswith(ext) for ext in _IMAGE_EXTS):
            return True
        ct = (getattr(att, "content_type", "") or "").lower()
        return ct.startswith("image/")
    except Exception:
        return False


def _is_webp(att: discord.Attachment) -> bool:
    try:
        if _ext(att.filename) == "webp":
            return True
        ct = (getattr(att, "content_type", "") or "").lower()
        return "webp" in ct
    except Exception:
        return False


def _is_png(att: discord.Attachment) -> bool:
    try:
        if _ext(att.filename) == "png":
            return True
        ct = (getattr(att, "content_type", "") or "").lower()
        return "png" in ct
    except Exception:
        return False


def _png_named_but_webp(att: discord.Attachment) -> bool:
    """Detect the common disguise pattern: filename endswith .png but content_type indicates WEBP."""
    try:
        fn_ext = _ext(att.filename)
        if fn_ext != "png":
            return False
        ct = (getattr(att, "content_type", "") or "").lower()
        if "webp" in ct:
            return True
        # Fallback: some attachments omit content_type; try URL hint
        url = (getattr(att, "url", "") or "").lower()
        return ".webp" in url
    except Exception:
        return False


def _has_url(text: str) -> bool:
    try:
        return bool(_URL_RE.search(text or ""))
    except Exception:
        return False


def _is_valid_webp_bytes(b: bytes) -> bool:
    # WEBP: RIFF....WEBP
    try:
        if not b or len(b) < 12:
            return False
        return b[0:4] == b"RIFF" and b[8:12] == b"WEBP"
    except Exception:
        return False


def _is_valid_png_bytes(b: bytes) -> bool:
    # PNG signature
    try:
        return bool(b) and len(b) >= 8 and b[:8] == b"\x89PNG\r\n\x1a\n"
    except Exception:
        return False


def _mass_blast_disguise(msg: discord.Message) -> bool:
    """High-confidence phishing blast:
    - @everyone ping
    - 4+ image attachments
    - at least one attachment is named .png but looks like webp by metadata
    """
    try:
        if not getattr(msg, "mention_everyone", False):
            # Support plain-text '@everyone' if Discord didn't mark mention
            if "@everyone" not in (getattr(msg, "content", "") or ""):
                return False
        imgs = [a for a in (getattr(msg, "attachments", None) or []) if _is_image_attachment(a)]
        if len(imgs) < 4:
            return False
        # Require PNG-looking filenames to match the user-requested pattern
        png_named = sum(1 for a in imgs if _ext(getattr(a, "filename", "")) == "png")
        if png_named < 4:
            return False
        disguised = any(_png_named_but_webp(a) for a in imgs)
        return bool(disguised)
    except Exception:
        return False


class FirstTouchdown(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enable = (os.getenv("FIRST_TOUCHDOWN_ENABLE", "0") == "1")
        self.chan = _ids(os.getenv("FIRST_TOUCHDOWN_CHANNELS", ""))
        self.bypass = _ids(os.getenv("FIRST_TOUCHDOWN_BYPASS_CHANNELS", "")) | _ids(os.getenv("PROTECT_CHANNEL_IDS", ""))
        self.ban_on_phash = (os.getenv("FIRST_TOUCHDOWN_BAN_ON_PHASH", "0") == "1")

        # Discord API expects 0..7 days.
        try:
            dd = int(os.getenv("PHISH_DELETE_MESSAGE_DAYS", "0") or "0")
        except Exception:
            dd = 0
        self.delete_days = max(0, min(int(dd), 7))

        log.info(
            "[first-touchdown] enable=%s channels=%s bypass=%s ban_on_phash=%s delete_days=%s",
            self.enable,
            sorted(self.chan),
            sorted(self.bypass),
            self.ban_on_phash,
            self.delete_days,
        )

    async def _fetch_message(self, channel: discord.abc.Messageable | None, message_id: int | None) -> discord.Message | None:
        if not channel or not message_id:
            return None
        try:
            if hasattr(channel, "fetch_message"):
                return await channel.fetch_message(int(message_id))
        except Exception:
            return None
        return None

    async def _validate_single_webp(self, msg: discord.Message) -> bool | None:
        """Return True if valid WEBP, False if definitely invalid, None if not applicable/unknown."""
        try:
            atts = [a for a in (getattr(msg, "attachments", None) or []) if _is_image_attachment(a)]
            if len(atts) != 1:
                return None
            a = atts[0]
            if not _is_webp(a):
                return None
            # Read bytes once; keep minimal checks.
            b = await a.read()
            if not b:
                return False
            if _is_valid_webp_bytes(b[:16]):
                return True
            # Some scams use extension mismatch; accept valid PNG too, but treat mismatch as suspicious.
            if _is_valid_png_bytes(b[:16]):
                return False
            return False
        except Exception:
            # Unknown; do not treat as invalid (avoid false ban)
            return None

    async def _ban_and_delete(
        self,
        guild: discord.Guild | None,
        channel: discord.abc.Messageable | None,
        user_id: int | None,
        message_id: int | None,
        reason: str,
    ):
        """Try to ban and delete message safely; non-blocking."""
        if not guild or not user_id:
            return
        try:
            if channel and isinstance(channel, discord.TextChannel) and channel.id in self.bypass:
                return  # never act in bypass/protect channels

            member = None
            if user_id:
                member = guild.get_member(user_id)
                if member is None:
                    with contextlib.suppress(Exception):
                        member = await guild.fetch_member(user_id)

            if member:
                with contextlib.suppress(Exception):
                    await guild.ban(member, reason=reason[:180], delete_message_days=self.delete_days)

            if channel and message_id and hasattr(channel, "fetch_message"):
                if isinstance(channel, discord.TextChannel) and channel.id in self.bypass:
                    return
                with contextlib.suppress(Exception):
                    msg = await channel.fetch_message(int(message_id))
                    try:
                        from nixe.helpers import phish_evidence_cache as _pec
                        _pec.record_message(msg, provider="first-touchdown", reason=str(reason or "")[:180])
                    except Exception:
                        pass
                    await msg.delete()
        except Exception:
            pass

    async def _delete_only(self, channel: discord.abc.Messageable | None, message_id: int | None, *, reason: str = "") -> None:
        try:
            if not channel or not message_id or not hasattr(channel, "fetch_message"):
                return
            msg = await channel.fetch_message(int(message_id))
            try:
                from nixe.helpers import phish_evidence_cache as _pec
                _pec.record_message(msg, provider="first-touchdown", reason=str(reason or "")[:180])
            except Exception:
                pass
            with contextlib.suppress(Exception):
                await msg.delete()
        except Exception:
            return

    @commands.Cog.listener("on_nixe_phish_detected")
    async def on_nixe_phish_detected(self, payload: dict):
        if not self.enable:
            return
        try:
            cid = int(payload.get("channel_id") or 0)
            gid = int(payload.get("guild_id") or 0)
            uid = int(payload.get("user_id") or 0)
            mid = int(payload.get("message_id") or 0)

            if not (cid and cid in self.chan and cid not in self.bypass):
                return

            provider = str(payload.get("provider") or "").lower().strip()
            kind = str(payload.get("kind") or "").lower().strip()
            is_image_hash_signal = any(k in provider for k in ("phash", "dhash", "hash", "image")) or kind.startswith("phash")

            guild = self.bot.get_guild(gid) if gid else None
            channel = self.bot.get_channel(cid) if cid else None

            msg = await self._fetch_message(channel, mid)
            if msg:
                # Best-effort evidence cache for ban embed
                try:
                    from nixe.helpers import phish_evidence_cache as _pec
                    _pec.record_message(msg, provider="first-touchdown", reason="FirstTouchdown: phishing detected")
                except Exception:
                    pass

                # Fast-path ban: @everyone blast + 4 PNGs disguised as WEBP
                if _mass_blast_disguise(msg):
                    await self._ban_and_delete(guild, channel, uid, mid, "FirstTouchdown: mass mention + multi-image disguise")
                    return

                # Safety gate: single valid WEBP + image-hash-only signal => delete-only (avoid false-positive ban)
                webp_valid = await self._validate_single_webp(msg)
                if webp_valid is True and is_image_hash_signal:
                    # If there are other high-confidence indicators, keep ban.
                    if not getattr(msg, "mention_everyone", False) and not _has_url(getattr(msg, "content", "") or ""):
                        await self._delete_only(channel, mid, reason="FirstTouchdown: single WEBP gated (delete-only)")
                        return
                # If WEBP is invalid/unknown, do not *upgrade* to ban solely on that; continue default behavior.

            # Default behavior: ban + delete
            try:
                from nixe.helpers import phish_evidence_cache as _pec
                _pec.record_from_payload(payload, provider="first-touchdown", reason="FirstTouchdown: phishing detected")
            except Exception:
                pass
            await self._ban_and_delete(guild, channel, uid, mid, "FirstTouchdown: phishing detected")
        except Exception:
            pass

    @commands.Cog.listener("on_nixe_phash_match")
    async def on_nixe_phash_match(self, payload: dict):
        if not (self.enable and self.ban_on_phash):
            return
        try:
            cid = int(payload.get("channel_id") or 0)
            gid = int(payload.get("guild_id") or 0)
            uid = int(payload.get("user_id") or 0)
            mid = int(payload.get("message_id") or 0)

            if not (cid and cid in self.chan and cid not in self.bypass):
                return

            guild = self.bot.get_guild(gid) if gid else None
            channel = self.bot.get_channel(cid) if cid else None
            msg = await self._fetch_message(channel, mid)
            if msg:
                try:
                    from nixe.helpers import phish_evidence_cache as _pec
                    _pec.record_message(msg, provider="first-touchdown", reason="FirstTouchdown: image match")
                except Exception:
                    pass

                if _mass_blast_disguise(msg):
                    await self._ban_and_delete(guild, channel, uid, mid, "FirstTouchdown: mass mention + multi-image disguise")
                    return

                webp_valid = await self._validate_single_webp(msg)
                if webp_valid is True:
                    if not getattr(msg, "mention_everyone", False) and not _has_url(getattr(msg, "content", "") or ""):
                        await self._delete_only(channel, mid, reason="FirstTouchdown: single WEBP gated (delete-only)")
                        return

            try:
                from nixe.helpers import phish_evidence_cache as _pec
                _pec.record_from_payload(payload, provider="first-touchdown", reason="FirstTouchdown: image match")
            except Exception:
                pass

            await self._ban_and_delete(guild, channel, uid, mid, "FirstTouchdown: image match")
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(FirstTouchdown(bot))
