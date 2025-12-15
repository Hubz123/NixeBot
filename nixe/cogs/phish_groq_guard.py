# nixe/cogs/phish_groq_guard.py
from __future__ import annotations

import os, logging, asyncio, json, re
import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger("nixe.cogs.phish_groq_guard")

from nixe.helpers.ban_utils import emit_phish_detected

PHISH_MIN_BYTES = int(os.getenv("PHISH_MIN_IMAGE_BYTES", "8192"))

def _env_set(*names: str) -> set[int]:
    out: set[int] = set()
    for n in names:
        raw = (os.getenv(n, "") or "").replace(";", ",")
        for part in raw.split(","):
            part = (part or "").strip()
            if part.isdigit():
                out.add(int(part))
    return out

# Channels to actively guard (used only when PHISH_GUARD_ALL_CHANNELS=0)
GUARD_IDS = _env_set("LPG_GUARD_CHANNELS", "PROTECT_CHANNEL_IDS", "PHISH_GUARD_CHANNELS")

# Channels/threads where the phishing guard must NEVER act
SKIP_IDS = _env_set("PHISH_SKIP_CHANNELS", "PHASH_MATCH_SKIP_CHANNELS")

GUARD_ALL = (os.getenv("PHISH_GUARD_ALL_CHANNELS", "1").strip().lower() in ("1", "true", "yes", "on"))

# Scan multiple attachments (prevents bypass when the first attachment is benign)
SCAN_MAX_IMAGES = int(os.getenv("PHISH_GROQ_SCAN_MAX_IMAGES", "4"))
VISION_MIN_CONF = float(os.getenv("PHISH_GROQ_VISION_MIN_CONF", "0.82"))
WEBP_MIN_CONF = float(os.getenv("PHISH_GROQ_WEBP_MIN_CONF", "0.92"))

if not SKIP_IDS:
    # Default: mod channels to exclude from phishing guards
    SKIP_IDS = {1400375184048787566, 936690788946030613}

try:
    _safe_tid = int(
        os.getenv("PHISH_DATA_THREAD_ID")
        or os.getenv("NIXE_PHISH_DATA_THREAD_ID")
        or os.getenv("PHASH_IMAGEPHISH_THREAD_ID")
        or "0"
    )
    if _safe_tid:
        GUARD_IDS.add(_safe_tid)
except Exception:
    pass

def _resolve_groq_key() -> str:
    # Prefer canonical single key
    key = (os.getenv("GROQ_API_KEY") or "").strip()
    if key:
        return key
    # Support comma-separated pool
    pooled = (os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_KEYS") or "").strip()
    if pooled:
        for part in pooled.split(","):
            k = part.strip()
            if k:
                return k
    # Support numbered keys (GROQ_API_KEY_1..n)
    for i in range(1, 21):
        k = (os.getenv(f"GROQ_API_KEY_{i}") or "").strip()
        if k:
            return k
    return ""
MODEL_VISION = os.getenv("GROQ_MODEL_VISION") or "meta-llama/llama-4-scout-17b-16e-instruct"
LOG_CHAN_ID = int(os.getenv("PHISH_LOG_CHAN_ID") or os.getenv("NIXE_PHISH_LOG_CHAN_ID") or "0")
TIMEOUT_MS = int(os.getenv("PHISH_GEMINI_MAX_LATENCY_MS", "12000"))
ENABLE = (os.getenv("PHISH_GROQ_ENABLE", "1") == "1")


def _ext(name: str) -> str:
    name = (name or "").lower().strip()
    if "." in name:
        return "." + name.split(".")[-1]
    return ""


def _is_image_attachment(a: discord.Attachment) -> bool:
    ct = (getattr(a, "content_type", "") or "").lower()
    name = (getattr(a, "filename", "") or "").lower()
    return ct.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"))


def _is_webp_attachment(att: discord.Attachment) -> bool:
    ct = (getattr(att, "content_type", "") or "").lower()
    name = (getattr(att, "filename", "") or "").lower()
    return ct == "image/webp" or name.endswith(".webp")


_WEBP_ACTION_KWS = (
    "login", "log in", "sign in", "password", "passcode", "otp", "2fa", "verification",
    "verify", "redeem", "claim", "activate", "qr", "qr code", "scan", "confirm", "continue",
)


def _webp_signals_ok(signals: list[str], reason: str) -> bool:
    """
    WEBP is unusually prone to model false-positives on memes/artwork.
    Only allow action when the model also surfaced at least one strong signal keyword.
    """
    blob = " ".join([*(signals or []), (reason or "")]).lower()
    return any(k in blob for k in _WEBP_ACTION_KWS)


def _sus(att: discord.Attachment) -> bool:
    """
    Decide whether an attachment is suspicious enough to send to Groq.

    For WEBP we use stricter heuristics to keep precision high; pHash guard still covers
    known-bad WEBP via the imagephish database.
    """
    ct = (getattr(att, "content_type", "") or "").lower()
    size = int(getattr(att, "size", 0) or 0)
    ext = _ext(getattr(att, "filename", "") or "")

    w = int(getattr(att, "width", 0) or 0)
    h = int(getattr(att, "height", 0) or 0)

    is_webp = (ct == "image/webp" or ext == ".webp")

    # Always treat heavy TIFF/BMP payloads as suspicious (classic QR/login bait)
    if ext in {".tiff", ".bmp"}:
        return True

    # WEBP: always send to Groq once it is a real image (coverage),
    # but we will apply stricter *action* gating later to prevent false positives.
    if is_webp:
        if size and size < PHISH_MIN_BYTES:
            return False
        return True

    # Small images are almost never scams (precision > recall)
    if size and size < PHISH_MIN_BYTES:
        return False

    # Screenshots and "phone screen" aspect ratios are higher risk
    if w and h:
        long_side = max(w, h)
        short_side = min(w, h)
        if long_side >= 900 and (long_side / max(short_side, 1)) >= 1.25:
            return True

    # Large payloads can still be risky even without dimensions
    if size >= PHISH_MIN_BYTES * 4:
        return True

    # Fallback: still allow scanning generic large images when guard-all is enabled.
    if GUARD_ALL and size >= PHISH_MIN_BYTES * 4:
        return True

    return False




def _sort_key(att: discord.Attachment) -> tuple[int, int]:
    """Sort larger / more "screenshot-like" images first.

    This reduces bypass probability when multiple attachments are posted.
    """
    try:
        w = int(getattr(att, "width", 0) or 0)
        h = int(getattr(att, "height", 0) or 0)
        px = w * h if (w and h) else 0
    except Exception:
        px = 0
    try:
        size = int(getattr(att, "size", 0) or 0)
    except Exception:
        size = 0
    return (px, size)



class GroqPhishGuard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        log.info(
            "[phish-groq] enable=%s model=%s guard_all=%s scan_max=%s min_conf=%.2f guard_ids=%s skip_ids=%s",
            ENABLE,
            MODEL_VISION,
            GUARD_ALL,
            SCAN_MAX_IMAGES,
            VISION_MIN_CONF,
            sorted(GUARD_IDS),
            sorted(SKIP_IDS),
        )

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        groq_key = _resolve_groq_key()
        if not ENABLE or not groq_key:
            return
        try:
            if message.author.bot:
                return
            ch = getattr(message, "channel", None)
            if not ch:
                return
            parent = getattr(ch, "parent", None)

            # Keep existing Forum short-circuit (handled by other pipelines in some servers)
            try:
                from discord import ForumChannel  # type: ignore
            except Exception:
                ForumChannel = None
            if ForumChannel and (isinstance(ch, ForumChannel) or isinstance(parent, ForumChannel)):
                return
            ctype = getattr(ch, "type", None)
            ptype = getattr(parent, "type", None)
            if any("forum" in str(t).lower() for t in (ctype, ptype)):
                return

            cid = int(getattr(ch, "id", 0) or 0)
            pid = int(getattr(ch, "parent_id", 0) or 0)
            if cid in SKIP_IDS or (pid and pid in SKIP_IDS):
                return
            if (not GUARD_ALL) and not ((cid in GUARD_IDS) or (pid and pid in GUARD_IDS)):
                return

            # Collect ALL image attachments, then scan suspicious ones (prevents multi-attachment bypass)
            imgs = [a for a in (message.attachments or []) if _is_image_attachment(a)]
            if not imgs:
                return

            candidates = [a for a in imgs if _sus(a)]
            if not candidates:
                return

            candidates.sort(key=_sort_key, reverse=True)
            candidates = candidates[: max(1, min(SCAN_MAX_IMAGES, 8))]

            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}

            prompt = (
                "You are a high-precision phishing/scam detector for Discord images.\n"
                "Classify the image as phish=true ONLY if there are clear indicators of a scam, such as:\n"
                "- impersonation of brands/admins;\n"
                "- requests for passwords/OTP/verification;\n"
                "- 'bonus', 'activate code', 'redeem', 'claim', 'free money', gambling/casino/crypto giveaway bait;\n"
                "- QR codes or instructions to contact unknown accounts to receive rewards;\n"
                "- any deceptive call-to-action that aims to trick users into unsafe actions.\n\n"
                "If it is a normal photo, artwork, meme, or harmless promo with no deceptive intent, return phish=false.\n"
                "Return STRICT JSON only: "
                '{"phish":true/false, "confidence":0.0-1.0, "reason":"short", "signals":["..."]}.'
            )

            best = {"phish": False, "confidence": 0.0, "reason": "", "att": None, "signals": []}

            timeout = aiohttp.ClientTimeout(total=TIMEOUT_MS / 1000.0)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                for att in candidates:
                    payload = {
                        "model": MODEL_VISION,
                        "temperature": 0.0,
                        "max_tokens": 200,
                        "messages": [
                            {"role": "system", "content": "You are a strict, high-precision phishing detector."},
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {"type": "image_url", "image_url": {"url": getattr(att, "url", None)}},
                                ],
                            },
                        ],
                    }
                    try:
                        async with sess.post(url, headers=headers, json=payload) as resp:
                            data = await resp.json(content_type=None)
                    except Exception as e:
                        log.debug("[phish-groq] http err: %r", e)
                        continue

                    txt = ""
                    try:
                        txt = (data["choices"][0]["message"]["content"] or "").strip()
                    except Exception:
                        txt = ""

                    # Parse JSON response
                    phish = False
                    conf = 0.0
                    reason = (txt or "")[:200]
                    signals = []

                    try:
                        m = re.search(r"\{.*\}", txt, flags=re.S)
                        obj = json.loads(m.group(0) if m else txt)
                        if isinstance(obj, dict):
                            phish = bool(obj.get("phish", False))
                            try:
                                conf = float(obj.get("confidence", 0.0) or 0.0)
                            except Exception:
                                conf = 0.0
                            if "reason" in obj:
                                reason = str(obj.get("reason"))[:200]
                            sig = obj.get("signals", [])
                            if isinstance(sig, list):
                                signals = [str(x)[:64] for x in sig][:8]
                    except Exception:
                        # Fallback: only treat as phish if it explicitly states phish true
                        lower = (txt or "").lower()
                        if re.search(r'"phish"\s*:\s*true', lower):
                            phish = True
                            conf = max(conf, 0.7)

                    if phish and conf > best["confidence"]:
                        best = {"phish": True, "confidence": conf, "reason": reason, "att": att, "signals": signals}

                    # Fast exit on high-confidence hit
                    if phish and conf >= VISION_MIN_CONF:
                        best = {"phish": True, "confidence": conf, "reason": reason, "att": att, "signals": signals}
                        break

            if not best["phish"]:
                return

            hit_att = best["att"]
            required_conf = VISION_MIN_CONF

            # WEBP is scanned, but we apply stricter action criteria to avoid false positives.
            if hit_att is not None and _is_webp_attachment(hit_att):
                required_conf = max(VISION_MIN_CONF, WEBP_MIN_CONF)
                if float(best["confidence"]) < required_conf:
                    return
                if not _webp_signals_ok(list(best.get("signals") or []), str(best.get("reason") or "")):
                    return
            else:
                if float(best["confidence"]) < required_conf:
                    return

            hit_url = getattr(hit_att, "url", None) if hit_att else None

            ev_urls = []
            try:
                ev_urls = [getattr(a, "url", None) for a in (getattr(message, "attachments", []) or [])]
                ev_urls = [x for x in ev_urls if x]
            except Exception:
                ev_urls = []

            details = {
                "score": float(best["confidence"]),
                "provider": "groq",
                "reason": best["reason"],
                "kind": "image",
                "signals": best.get("signals") or [],
                "image_url": hit_url,
                "evidence_urls": ev_urls,
            }

            emit_phish_detected(self.bot, message, details)

        except Exception as e:
            log.debug("[phish-groq] err: %r", e)

    @commands.Cog.listener("on_message_edit")
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Re-run Groq vision phishing checks on edited messages
        try:
            await self.on_message(after)
        except Exception as e:
            log.debug("[phish-groq] edit err: %r", e)


async def setup(bot: commands.Bot):
    await bot.add_cog(GroqPhishGuard(bot))
