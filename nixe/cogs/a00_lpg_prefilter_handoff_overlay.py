# -*- coding: utf-8 -*-
"""
Prefilter handoff for phishing (robust fake-attachment detection).
- Sniffs magic bytes to detect real format (WEBP/HEIC/PDF/SVG/etc), even if filename says .png or content_type is image/png.
- If mismatch or suspicious format, signals GROQ phishing guard (without touching env/config).
"""
from __future__ import annotations
import os, logging, asyncio, io
import discord
from discord.ext import commands

log = logging.getLogger("nixe.cogs.a00_lpg_prefilter_handoff_overlay")

_MAX_HEADER = 65536  # 64KB
_SUS_FORMATS = {"webp","heic","avif","svg","pdf","tiff","bmp"}

def _ext(name: str) -> str:
    name = (name or "").lower().strip()
    if "." in name: return "." + name.split(".")[-1]
    return ""

def _sniff_fmt(head: bytes) -> str:
    b = head
    try:
        if b.startswith(b"\x89PNG\r\n\x1a\n"): return "png"
        if b.startswith(b"\xff\xd8\xff"): return "jpeg"
        if b.startswith(b"GIF87a") or b.startswith(b"GIF89a"): return "gif"
        if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "webp"
        if b[:4] == b"%PDF": return "pdf"
        if b[:4] == b"II*\x00" or b[:4] == b"MM\x00*": return "tiff"
        if b.startswith(b"BM"): return "bmp"
        if b[:12].lower().startswith(b"\x00\x00\x00\x18ftyp") or b[:12].lower().startswith(b"\x00\x00\x00\x20ftyp"):
            # Quick check for HEIC/AVIF (ISO BMFF)
            if b[8:12].lower() in (b"ftyp",): 
                tail = b[12:32].lower()
                if b"heic" in tail or b"heif" in tail: return "heic"
                if b"avif" in tail: return "avif"
        # SVG (xml); naive but effective within header bytes
        if b.lstrip().lower().startswith(b"<svg") or b.lstrip().lower().startswith(b"<!doctype svg"):
            return "svg"
    except Exception:
        pass
    return "unknown"

def _sus_by_mismatch(filename: str, content_type: str, actual: str) -> bool:
    ext = _ext(filename).lstrip(".")
    ct = (content_type or "").lower()
    # It's suspicious if:
    # - actual format differs from extension or declared content-type
    # - or actual format is in known phishing-bait set (webp/heic/avif/svg/pdf/tiff/bmp)
    if actual in _SUS_FORMATS: 
        if ext and ext != actual: 
            return True
        if ct and (actual not in ct):
            return True
        # even if they match, these formats are still high-risk -> flag
        return True
    # if declared png/jpeg but header says unknown -> flag
    if ext in ("png","jpg","jpeg") or ("image/png" in ct or "image/jpeg" in ct):
        if actual not in ("png","jpeg"):
            return True
    return False

class LPGPrefilterHandoff(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # keep behavior fully controlled by existing flags; do not add new env
        self.enable = (os.getenv("PHISH_GROQ_ENABLE","1") == "1")
        self.guard_ids = set(int(x) for x in (os.getenv("LPG_GUARD_CHANNELS","") or "").replace(";",",").split(",") if x.strip().isdigit())
        log.info("[lpg-prefilter] enable=%s guard_ids=%s", self.enable, sorted(self.guard_ids))

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if not self.enable: 
            return
        try:
            if not message or (getattr(message,"author",None) and message.author.bot):
                return
            ch = getattr(message, "channel", None)
            if not ch: return
            cid = int(getattr(ch, "id", 0) or 0)
            pid = int(getattr(ch, "parent_id", 0) or 0)
            in_guard = (cid in self.guard_ids) or (pid and pid in self.guard_ids)
            if not in_guard:
                return

            # Find first attachment (image-like or any that pretends to be image.*)
            att = None
            for a in message.attachments or []:
                name = getattr(a,"filename","") or ""
                ct = (getattr(a,"content_type","") or "").lower()
                if ct.startswith("image/") or _ext(name) in (".png",".jpg",".jpeg",".webp",".gif",".bmp",".tiff",".svg",".avif",".heic"):
                    att = a; break
            if not att:
                return

            # Download small header bytes to sniff actual format
            head = b""
            try:
                data = await att.read()
                head = data[:_MAX_HEADER]
            except Exception:
                # if we can't read, don't block the flow; bail out
                return
            actual = _sniff_fmt(head)
            name = getattr(att,"filename","") or ""
            ctype = (getattr(att,"content_type","") or "").lower()

            if _sus_by_mismatch(name, ctype, actual):
                # Wake GROQ pipeline (without changing config)
                log.warning("[lpg-prefilter] FAKE-ATTACH? name=%s ct=%s actual=%s -> signal GROQ", name, ctype, actual)
                try:
                    self.bot.dispatch("nixe_unusual_image_prefilter", message, {"name": name, "ct": ctype, "actual": actual})
                except Exception:
                    pass
                # optional: warmup Groq once so cold-start won't RTO
                if os.getenv("PHISH_WARMUP_GROQ","1") == "1":
                    asyncio.create_task(self._warmup_groq())
        except Exception as e:
            log.debug("[lpg-prefilter] err: %r", e)

    async def _warmup_groq(self):
        try:
            import aiohttp, os
            timeout = aiohttp.ClientTimeout(total=3.0)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                url = "https://api.groq.com/openai/v1/models"
                headers = {"Authorization": f"Bearer {os.getenv('GROQ_API_KEY','')}"}
                async with sess.get(url, headers=headers) as resp:
                    await resp.text()
            log.info("[lpg-prefilter] GROQ warmup ok")
        except Exception:
            pass

async def setup(bot):
    await bot.add_cog(LPGPrefilterHandoff(bot))
