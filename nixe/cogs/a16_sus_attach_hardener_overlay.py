# -*- coding: utf-8 -*-
from __future__ import annotations
import os, re, asyncio, logging, urllib.parse
from typing import Tuple, Optional, List
import discord
from discord.ext import commands

log = logging.getLogger("nixe.cogs.a16_sus_attach_hardener_overlay")

_IMG_EXT = {".png",".jpg",".jpeg",".gif",".bmp",".webp",".jfif",".pjpeg",".pjp",".tif",".tiff"}
_ARCHIVE_EXT = {".zip",".rar",".7z",".tar",".gz",".xz"}
_EXEC_LIKE = {".exe",".scr",".bat",".cmd",".msi",".js",".vbs",".ps1",".lnk"}
_DOC_EXT = {".pdf",".doc",".docx",".xls",".xlsx",".ppt",".pptx"}

# --- Content-level phishing hints (message text & embed URLs) ---
_SUS_WORDS = [
    r"nitro", r"discord\.gift", r"gift", r"airdrop", r"giveaway", r"free", r"bonus",
    r"steam.*gift", r"robux", r"verification", r"verify.*account", r"2fa", r"otp",
    r"wallet", r"metamask", r"seed\s*phrase", r"pass\s*phrase", r"private\s*key",
    r"connect\s*wallet", r"claim\s*reward", r"login", r"signin", r"web3", r"binance",
    r"trust[\s-]*wallet", r"exchange", r"promo", r"limited", r"urgent", r"appeal", r"suspend",
    r"xn--", r"0auth", r"0tp", r"0rg", r"discorcl", r"disc0rd", r"stean", r"steaṁ"
]
_SUS_RE = re.compile(r"(?i)(" + r"|".join(_SUS_WORDS) + r")")

def _ext(name: str) -> str:
    n = (name or "").lower().strip()
    m = re.search(r"(\.[a-z0-9]{1,5})$", n)
    return m.group(1) if m else ""

def _has_double_ext(name: str) -> bool:
    n = (name or "").lower()
    return bool(re.search(r"\.(?:png|jpe?g|gif|bmp|webp|pdf)\.(?:exe|scr|bat|cmd|js|vbs|ps1|lnk)$", n))

def _sniff(buf: bytes) -> str:
    b = buf or b""
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "image/webp"
    if b.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if b[:2] == b"\xff\xd8": return "image/jpeg"
    if b[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
    if b[:2] == b"BM": return "image/bmp"
    if len(b) >= 12 and b[4:8] == b"ftyp": return "video/mp4"   # mp4 boxed format
    if b[:4] == b"%PDF": return "application/pdf"
    if b[:4] == b"PK\x03\x04": return "application/zip"
    if b[:6] == b"7z\xbc\xaf\x27\x1c": return "application/x-7z-compressed"
    if b[:4] == b"Rar!": return "application/x-rar-compressed"
    return "application/octet-stream"

async def _download(url: str, limit: int = 5_000_000) -> Optional[bytes]:
    if not url or not url.startswith("http"): return None
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=10) as r:
                if r.status == 200:
                    b = await r.read()
                    return b[:limit]
    except Exception:
        try:
            import requests, asyncio as _asyncio
            b = await _asyncio.to_thread(lambda: requests.get(url, timeout=10).content)
            return b[:limit]
        except Exception:
            return None
    return None

def _text_ratio(buf: bytes) -> float:
    if not buf: return 0.0
    total = min(len(buf), 20000)
    sample = buf[:total]
    letters = sum(ch in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789" for ch in sample)
    return letters / float(total or 1)

def _score_attachment(name: str, buf: bytes) -> Tuple[int, str, str]:
    score = 0
    reason = []
    ext = _ext(name)
    mime = _sniff(buf)

    if _has_double_ext(name):
        score += 3; reason.append("double-ext")

    if ext in _IMG_EXT and mime not in ("image/png","image/jpeg","image/gif","image/webp","image/bmp"):
        score += 2; reason.append(f"ext-image/mime-{mime}")

    if ext in _ARCHIVE_EXT and mime not in ("application/zip","application/x-7z-compressed","application/x-rar-compressed"):
        score += 1; reason.append(f"archive-mime-mismatch:{mime}")

    if ext in _EXEC_LIKE:
        score += 3; reason.append("exec-like-ext")

    if ext == ".png" and mime == "image/webp":
        score += 1; reason.append("png-as-webp")

    # if looks like generic binary for an image ext → suspicious
    if ext in _IMG_EXT and mime == "application/octet-stream":
        score += 2; reason.append("image-ext/octet-stream")

    if len(buf or b"") <= 200:
        score += 1; reason.append("tiny-bytes")
    if mime.startswith("image/") and _text_ratio(buf) > 0.35:
        score += 1; reason.append("text-heavy-image")

    return score, ",".join(reason) or "ok", mime

def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""

def _content_signals(message: discord.Message) -> Tuple[int,str]:
    score = 0
    reasons = []
    try:
        content = (message.content or "").lower()
        if content and _SUS_RE.search(content):
            score += 1; reasons.append("sus-words")
    except Exception:
        pass
    try:
        for e in getattr(message, "embeds", []) or []:
            for u in [getattr(e, "url", None), getattr(getattr(e, "image", None), "url", None),
                      getattr(getattr(e, "thumbnail", None), "url", None)]:
                if not u: continue
                h = _host(u)
                if "discord.com" in h or "discordapp.com" in h: continue
                if "tenor.co" in h or "tenor.com" in h or "media.tenor.com" in h: continue
                if "giphy.com" in h or "gyazo.com" in h or "imgur.com" in h: continue
                # Punycode or obvious lookalikes
                if "xn--" in h or re.search(r"(disc0rd|dlscord|stea[mrn]|0auth|0tp)", h):
                    score += 2; reasons.append(f"sus-host:{h}")
    except Exception:
        pass
    return score, ",".join(reasons) or "ok"

try:
    from nixe.helpers.gemini_bridge import classify_phish_image  # async
except Exception as e:
    log.debug("[sus-hard] gemini helper not available: %r", e)
    async def classify_phish_image(images, hints: str = "", timeout_ms: int = 10000):
        return "benign", 0.0

class SusAttachHardener(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enable = bool(int(os.getenv("SUS_ATTACH_HARDENER_ENABLE", os.getenv("SUS_ATTACH_ENABLE", "1"))))
        self.delete_threshold = int(os.getenv("SUS_ATTACH_DELETE_THRESHOLD", "3"))
        self.max_bytes = int(os.getenv("SUS_ATTACH_MAX_BYTES", "5000000"))
        # Verbose logging is intentionally OFF by default to avoid log spam.
        # Enable explicitly via SUS_ATTACH_LOG_VERBOSE=1 if you want per-event logs.
        self.verbose = bool(int(os.getenv("SUS_ATTACH_LOG_VERBOSE", "0")))
        # Gemini assist
        self.gem_enable = bool(int(os.getenv("SUS_ATTACH_GEMINI_ENABLE", "1")))
        self.gem_thr = float(os.getenv("SUS_ATTACH_GEMINI_THRESHOLD", "0.85"))  # tightened default
        self.gem_timeout = int(os.getenv("SUS_ATTACH_GEM_TIMEOUT_MS", os.getenv("PHISH_GEMINI_MAX_LATENCY_MS", "12000")))
        self.gem_hints = os.getenv("SUS_ATTACH_GEMINI_HINTS", "login page, connect wallet, claim reward, giveaway, OTP request, QR login, suspicious URL, brand impersonation, seed phrase")
        # Force model check always (strict mode)
        self.always_gem = bool(int(os.getenv("SUS_ATTACH_ALWAYS_GEM", "1")))  # tightened default ON
        # Content scanning
        self.content_scan = bool(int(os.getenv("SUS_ATTACH_CONTENT_SCAN_ENABLE", "1")))
        self.ignore_channels = set((os.getenv("SUS_ATTACH_IGNORE_CHANNELS","") or "").replace(";",",").split(",")) - {""}
        log.warning("[sus-hard] enable=%s thr=%s gem=%s/%.2f always=%s content=%s ignore=%s",
                    self.enable, self.delete_threshold, self.gem_enable, self.gem_thr, self.always_gem, self.content_scan, sorted(self.ignore_channels))

    async def _collect_images(self, message: discord.Message) -> List[bytes]:
        blobs: List[bytes] = []
        for att in getattr(message, "attachments", []) or []:
            try:
                if isinstance(att, discord.Attachment) and (att.filename or "").lower().endswith(tuple(_IMG_EXT)):
                    b = await att.read()
                    if b: blobs.append(b)
            except Exception: pass
        for emb in getattr(message, "embeds", []) or []:
            try:
                url = ""
                if emb.image and emb.image.url: url = emb.image.url
                elif emb.thumbnail and emb.thumbnail.url: url = emb.thumbnail.url
                if url.startswith("http"):
                    b = await _download(url, self.max_bytes)
                    if b: blobs.append(b)
            except Exception: pass
        return blobs[:2]

    @commands.Cog.listener("on_message")
    async def _on_message(self, message: discord.Message):
        try:
            if not self.enable: return
            if message.author.bot: return
            if self.ignore_channels and str(message.channel.id) in self.ignore_channels: return
        except Exception:
            return

        # 1) Content signal
        total_score = 0
        reasons = []
        if self.content_scan:
            sc, rs = _content_signals(message)
            total_score += sc
            if sc: reasons.append(rs)
            # Only emit warnings when something is suspicious.
            if sc:
                log.warning("[sus-hard] content-score=%s reasons=%s", sc, rs)
            elif self.verbose:
                log.debug("[sus-hard] content-score=%s reasons=%s", sc, rs)

        # 2) Attachment scan
        for att in getattr(message, "attachments", []) or []:
            try:
                if not isinstance(att, discord.Attachment): continue
                name = att.filename or ""
                b = await att.read()
                sc, rs, mime = _score_attachment(name, b or b"")
                total_score += sc
                if sc: reasons.append(f"{name}:{rs}")
                # Only warn on suspicious attachments (score>0). Otherwise keep quiet.
                if sc:
                    log.warning("[sus-hard] att-score=%s mime=%s name=%s reasons=%s", sc, mime, name, rs)
                elif self.verbose:
                    log.debug("[sus-hard] att-score=%s mime=%s name=%s", sc, mime, name)
            except Exception as e:
                log.debug("[sus-hard] att err: %r", e)

        # 3) Gemini (always, or when below threshold)
        try_gem = self.gem_enable and (total_score < self.delete_threshold or self.always_gem)

        if try_gem:
            imgs = await self._collect_images(message)
            if imgs:
                if self.verbose:
                    log.debug("[sus-hard] gem try: imgs=%d thr=%.2f timeout=%dms", len(imgs), self.gem_thr, self.gem_timeout)
                try:
                    label, conf = await classify_phish_image(imgs, hints=self.gem_hints, timeout_ms=self.gem_timeout)
                    if label == "phish" and conf >= self.gem_thr:
                        total_score = max(total_score, self.delete_threshold)
                        reasons.append(f"gemini-phish@{conf:.2f}")
                        log.warning("[sus-hard] gemini-phish detected: conf=%.3f thr=%.2f", conf, self.gem_thr)
                    elif self.verbose:
                        log.debug("[sus-hard] gem classify: (%s, %.3f) thr=%.2f", label, conf, self.gem_thr)
                except Exception as e:
                    log.warning("[sus-hard] gem error: %r", e)
            else:
                if self.verbose:
                    log.debug("[sus-hard] gem skip: no images collected")

        if total_score >= self.delete_threshold:
            try:
                await message.delete()
                log.warning("[sus-hard] deleted in %s (score=%s reasons=%s)", message.channel.id, total_score, ";".join(reasons))
            except Exception as e:
                log.warning("[sus-hard] delete failed: %r", e)


async def setup(bot):
    await bot.add_cog(SusAttachHardener(bot))
