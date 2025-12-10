# nixe/cogs/phish_groq_guard.py
from __future__ import annotations
import os, logging, asyncio, json, re
import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger("nixe.cogs.phish_groq_guard")
from nixe.helpers.ban_utils import emit_phish_detected
PHISH_MIN_BYTES = int(os.getenv("PHISH_MIN_IMAGE_BYTES","8192"))
GUARD_IDS = set(int(x) for x in (os.getenv("LPG_GUARD_CHANNELS","") or "").replace(";",",").split(",") if x.strip().isdigit())
SKIP_IDS = set(int(x) for x in (os.getenv("PHISH_SKIP_CHANNELS","") or "").replace(";",",").split(",") if x.strip().isdigit())
if not SKIP_IDS:
    # Default: mod channels to exclude from phishing guards
    SKIP_IDS = {1400375184048787566, 936690788946030613}
try:
    _safe_tid = int(
        os.getenv("PHISH_DATA_THREAD_ID") or
        os.getenv("NIXE_PHISH_DATA_THREAD_ID") or
        os.getenv("PHASH_IMAGEPHISH_THREAD_ID") or "0")
    if _safe_tid:
        GUARD_IDS.add(_safe_tid)
except Exception:
    pass
GROQ_KEY = os.getenv("GROQ_API_KEY","")
MODEL_VISION = os.getenv("GROQ_MODEL_VISION") or "meta-llama/llama-4-scout-17b-16e-instruct"
LOG_CHAN_ID = int(os.getenv("PHISH_LOG_CHAN_ID") or os.getenv("NIXE_PHISH_LOG_CHAN_ID") or "0")
TIMEOUT_MS = int(os.getenv("PHISH_GEMINI_MAX_LATENCY_MS","12000"))
ENABLE = (os.getenv("PHISH_GROQ_ENABLE","1") == "1")

def _ext(name: str) -> str:
    name = (name or "").lower().strip()
    if "." in name: return "." + name.split(".")[-1]
    return ""

def _sus(att: discord.Attachment) -> bool:
    ct = (getattr(att,"content_type","") or "").lower()
    size = int(getattr(att,"size",0) or 0)
    if size < PHISH_MIN_BYTES: return False
    if ct in ("image/webp","image/tiff","image/bmp"): return True
    if _ext(getattr(att,"filename","")) in {".webp",".tiff",".bmp"}: return True
    # high-res images often used for QR/login — heuristic: extremely tall or wide not known here (no dims); rely on size≥min
    return False

class GroqPhishGuard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        log.info("[phish-groq] enable=%s model=%s guards=%s skip=%s", ENABLE, MODEL_VISION, sorted(GUARD_IDS), sorted(SKIP_IDS))

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if not ENABLE or not GROQ_KEY: return
        try:
            if message.author.bot: return
            ch = getattr(message,"channel",None)
            if not ch: return
            cid = int(getattr(ch,"id",0) or 0)
            pid = int(getattr(ch,"parent_id",0) or 0)
            if pid:
                return
            if cid in SKIP_IDS or (pid and pid in SKIP_IDS): 
                return
            if not ((cid in GUARD_IDS) or (pid and pid in GUARD_IDS)): 
                return

            # find one image
            att = None
            for a in message.attachments or []:
                if (getattr(a,"content_type","") or "").lower().startswith("image/"):
                    att = a; break
            if not att: return
            if not _sus(att): 
                return

            # Run Groq vision
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {GROQ_KEY}","Content-Type": "application/json"}
            prompt = ("Look at the image carefully and decide if it is being used as part of an online scam, phishing attempt, account takeover, fake investment or gambling promotion, or fake payout proof. Treat screenshots that show things like \"Withdrawal Success\", \"Deposit Successful\", big crypto balances, \"USDT received\", casino or broker apps, bonus banners, or payout proofs as phishing/scam content whenever they could be used to lure other people into depositing or signing up, even if no login form is shown. Also treat QR-code login, OTP/verification pages, or suspicious wallet/crypto screens as phishing. Reply ONLY with JSON: {\"phish\":true/false, \"reason\":\"short explanation\"}.")
            payload = {
                "model": MODEL_VISION,
                "temperature": 0.0,
                "max_tokens": 128,
                "messages": [
                    {"role":"system","content":"You are a strict phishing detector."},
                    {"role":"user","content":[
                        {"type":"text","text": prompt},
                        {"type":"image_url","image_url":{"url": getattr(att,'url', None)}}
                    ]}
                ]
            }
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_MS/1000.0)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(url, headers=headers, json=payload) as resp:
                    data = await resp.json(content_type=None)
            # parse result: prefer structured JSON, fallback to regex on text
            txt = ""
            try:
                txt = (data["choices"][0]["message"]["content"] or "").strip()
            except Exception:
                txt = ""
            is_phish = False
            reason = txt[:180]

            lower = txt.lower()
            # 1) Try to parse JSON object from the content
            try:
                start_brace = txt.find("{")
                end_brace = txt.rfind("}")
                json_slice = txt if start_brace == -1 or end_brace == -1 or end_brace <= start_brace else txt[start_brace : end_brace + 1]
                obj = json.loads(json_slice)
                if isinstance(obj, dict):
                    if "phish" in obj:
                        is_phish = bool(obj.get("phish"))
                    if "reason" in obj:
                        reason = str(obj.get("reason"))[:180]
            except Exception:
                # 2) Fallback: look for `"phish": true` in raw text
                try:
                    if re.search(r'"phish"\s*:\s*true', lower):
                        is_phish = True
                except Exception:
                    pass

            log.info("[phish-groq] result=%s reason=%s", is_phish, reason)

            try:
                if is_phish:
                    target_id = _safe_tid or LOG_CHAN_ID
                    if target_id:
                        logch = None
                        try:
                            logch = self.bot.get_channel(target_id) or await self.bot.fetch_channel(target_id)
                        except Exception:
                            logch = None
                        if logch:
                            msg_link = None
                            try:
                                gid = getattr(message.guild, "id", None)
                                cid = getattr(message.channel, "id", None)
                                mid = getattr(message, "id", None)
                                if gid and cid and mid:
                                    msg_link = f"https://discord.com/channels/{gid}/{cid}/{mid}"
                            except Exception:
                                msg_link = None
                            text = f"[phish-groq] sus image from <@{message.author.id}> → phish=True reason={reason}"
                            if msg_link:
                                text += f"\n{msg_link}"
                            await logch.send(text)

                    ev_urls = [getattr(att, 'url', None)] if getattr(att, 'url', None) else []
                    details = {
                        'score': 1.0,
                        'provider': 'groq',
                        'reason': reason,
                        'kind': 'image',
                    }
                    emit_phish_detected(self.bot, message, details, ev_urls)
            except Exception:
                pass
        except Exception as e:
            log.debug("[phish-groq] err: %r", e)


    @commands.Cog.listener("on_message_edit")
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Re-run Groq vision phishing checks on edited messages to prevent
        # editing a safe message into an image-based scam.
        try:
            await self.on_message(after)
        except Exception as e:
            log.debug("[phish-groq] edit err: %r", e)


async def setup(bot: commands.Bot):
    await bot.add_cog(GroqPhishGuard(bot))
