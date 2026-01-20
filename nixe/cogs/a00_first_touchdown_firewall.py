# -*- coding: utf-8 -*-
"""nixe.cogs.a00_first_touchdown_firewall

First Touchdown Firewall:
- On-message enforcement for high-risk patterns (phishing links and image blacklists).

Render free-plan safety hardening:
- Fast-path ban for '@everyone' blast + 4+ image attachments with PNG<->WEBP disguise.
- Avoid false-positive autobans for a single *valid* WEBP attachment by skipping image-hash enforcement
  unless additional high-confidence signals exist (e.g. phishing link or @everyone).

No config changes; reads env only.
"""

from __future__ import annotations

import re
import discord
from discord.ext import commands

from nixe.helpers.env_reader import get, get_int
from nixe.helpers.phash_tools import dhash_bytes, hamming
from nixe.helpers.phash_board import get_blacklist_hashes

URL_RE = re.compile(r"https?://[\w.-]+\.[a-z]{2,}(?:/\S*)?", re.I)
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

_PRESET_TEXT = {
    "suspicious": "Suspicious or spam account",
    "compromised": "Compromised or hacked account",
    "breaking": "Breaking server rules",
    "other": "Other",
}


def _ban_reason():
    preset = get("PHISH_BAN_PRESET", "suspicious").lower().strip()
    base = _PRESET_TEXT.get(preset, _PRESET_TEXT["suspicious"])
    custom = get("PHISH_BAN_REASON", "").strip()
    return f"{base} | {custom}" if preset == "other" and custom else base


def _delete_history_seconds():
    raw = get("PHISH_BAN_DELETE_HISTORY", "7d").lower().strip()
    if raw in ("none", "0", "no", "off"):
        return 0
    if raw.endswith("d"):
        try:
            return max(0, min(int(raw[:-1]) * 86400, 604800))
        except Exception:
            return 604800
    if raw.endswith("h"):
        try:
            return max(0, min(int(raw[:-1]) * 3600, 604800))
        except Exception:
            return 0
    try:
        v = int(raw)
        return max(0, min(v, 604800))
    except Exception:
        return 604800


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


def _png_named_but_webp(att: discord.Attachment) -> bool:
    """filename endswith .png but metadata hints WEBP"""
    try:
        if _ext(att.filename) != "png":
            return False
        ct = (getattr(att, "content_type", "") or "").lower()
        if "webp" in ct:
            return True
        url = (getattr(att, "url", "") or "").lower()
        return ".webp" in url
    except Exception:
        return False


def _is_valid_webp_prefix(b: bytes) -> bool:
    try:
        return bool(b) and len(b) >= 12 and b[0:4] == b"RIFF" and b[8:12] == b"WEBP"
    except Exception:
        return False


def _mass_blast_disguise(m: discord.Message) -> bool:
    try:
        if not getattr(m, "mention_everyone", False):
            if "@everyone" not in (m.content or ""):
                return False
        imgs = [a for a in (m.attachments or []) if _is_image_attachment(a)]
        if len(imgs) < 4:
            return False
        png_named = sum(1 for a in imgs if _ext(getattr(a, "filename", "")) == "png")
        if png_named < 4:
            return False
        return any(_png_named_but_webp(a) for a in imgs)
    except Exception:
        return False


class FirstTouchdownFirewall(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = get("PHISH_FTF_ENABLE", "1") == "1"
        self.guard = {int(x) for x in (get("PHISH_FTF_GUARD_CHANNELS", "").replace(",", " ").split()) if x.isdigit()}
        self.allow = {int(x) for x in (get("PHISH_FTF_ALLOW_CHANNELS", "").replace(",", " ").split()) if x.isdigit()}

        # Global skip channels (mods/safe); shared with other phish guards
        skip_raw = get("PHISH_SKIP_CHANNELS", "").replace(",", " ").split()
        self.skip = {int(x) for x in skip_raw if x.isdigit()}
        if not self.skip:
            # Default: mod channels that must never be auto-banned by FTF
            self.skip = {1400375184048787566, 936690788946030613}

        self.block = set(get("PHISH_BLOCK_DOMAINS", "").lower().replace(",", " ").split())
        self.hash_thr = int(get_int("PHISH_HASH_HAMMING_MAX", 6))
        self.hash_ref = get_blacklist_hashes()

    def _in_scope(self, ch_id: int) -> bool:
        if ch_id in self.skip:
            return False
        if self.allow and ch_id in self.allow:
            return False
        return (not self.guard) or (ch_id in self.guard)

    async def _banish(self, m: discord.Message, reason_suffix: str):
        reason = f"{_ban_reason()} • {reason_suffix}"
        secs = _delete_history_seconds()
        try:
            await m.guild.ban(m.author, reason=reason, delete_message_seconds=secs)
        except TypeError:
            days = min(7, secs // 86400)
            await m.guild.ban(m.author, reason=reason, delete_message_days=days)
        except Exception:
            try:
                await m.delete(reason=reason)
            except Exception:
                pass

    def _link_hit(self, content: str) -> bool:
        for match in URL_RE.finditer(content or ""):
            try:
                host = match.group(0).split("/")[2].lower()
            except Exception:
                continue
            if any(b and b in host for b in self.block):
                return True
        return False

    async def _single_webp_valid(self, m: discord.Message) -> bool | None:
        """Return True if single WEBP and signature OK, False if single WEBP but signature invalid, None otherwise."""
        try:
            imgs = [a for a in (m.attachments or []) if _is_image_attachment(a)]
            if len(imgs) != 1:
                return None
            a = imgs[0]
            if not _is_webp(a):
                return None
            # Safety: avoid huge downloads on Render.
            try:
                sz = int(getattr(a, "size", 0) or 0)
            except Exception:
                sz = 0
            if sz and sz > 8 * 1024 * 1024:
                return None
            b = await a.read()
            if not b:
                return False
            return True if _is_valid_webp_prefix(b[:16]) else False
        except Exception:
            return None

    async def _image_hit(self, m: discord.Message) -> bool:
        if not self.hash_ref:
            return False
        # Render safety: cap number of attachments we hash.
        checked = 0
        for a in m.attachments:
            if checked >= 2:
                break
            if not _is_image_attachment(a):
                continue
            n = (a.filename or "").lower()
            if not any(n.endswith(ext) for ext in _IMAGE_EXTS):
                # Still attempt if content_type indicates image
                ct = (getattr(a, "content_type", "") or "").lower()
                if not ct.startswith("image/"):
                    continue
            try:
                sz = int(getattr(a, "size", 0) or 0)
            except Exception:
                sz = 0
            if sz and sz > 8 * 1024 * 1024:
                continue
            try:
                b = await a.read()
            except Exception:
                continue
            checked += 1
            hv = dhash_bytes(b)
            if hv == 0:
                continue
            for ref in self.hash_ref:
                if hamming(hv, ref) <= self.hash_thr:
                    return True
        return False

    @commands.Cog.listener()
    async def on_message(self, m: discord.Message):
        if not self.enabled or m.author.bot:
            return

        ch = getattr(m, "channel", None)
        if not ch or not hasattr(ch, "id"):
            return

        try:
            cid = int(getattr(ch, "id", 0) or 0)
        except Exception:
            return

        # Threads: never apply first-touchdown firewall inside threads (forum or text).
        try:
            pid = int(getattr(ch, "parent_id", 0) or 0)
        except Exception:
            pid = 0
        if pid:
            return

        # Respect PHISH_SKIP_CHANNELS (mods/safe/boards/forums).
        if cid in self.skip or (pid and pid in self.skip):
            return
        if not self._in_scope(cid):
            return

        # High-confidence phishing blast: @everyone + 4 PNGs disguised as WEBP
        if m.attachments and _mass_blast_disguise(m):
            await self._banish(m, "mass mention + multi-image disguise")
            return

        # Link-based phishing is still high-confidence.
        if self._link_hit(m.content):
            await self._banish(m, "phishing link")
            return

        # Single WEBP safety gate: validate and avoid hash-ban unless extra signal exists.
        if m.attachments:
            webp_ok = await self._single_webp_valid(m)
            if webp_ok is False:
                # Invalid webp container: delete-only (avoid false-positive ban escalation).
                try:
                    await m.delete(reason="Invalid WEBP container (safety gate)")
                except Exception:
                    pass
                return
            if webp_ok is True:
                # Valid single WEBP with no other signal: do not hash-ban (avoid false positives).
                if not getattr(m, "mention_everyone", False):
                    return

        # Image blacklist enforcement (dHash) - capped for Render.
        if m.attachments and await self._image_hit(m):
            await self._banish(m, "phishing image")
            return


async def setup(bot: commands.Bot):
    await bot.add_cog(FirstTouchdownFirewall(bot))
