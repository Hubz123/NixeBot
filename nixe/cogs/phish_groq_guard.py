# nixe/cogs/phish_groq_guard.py
from __future__ import annotations
import os, logging, asyncio
import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger("nixe.cogs.phish_groq_guard")
from nixe.helpers.ban_utils import emit_phish_detected
PHISH_MIN_BYTES = int(os.getenv("PHISH_MIN_IMAGE_BYTES","8192"))
GUARD_IDS = set(int(x) for x in (os.getenv("LPG_GUARD_CHANNELS","") or "").replace(";",",").split(",") if x.strip().isdigit())
GROQ_KEY = os.getenv("GROQ_API_KEY","")
MODEL_VISION = os.getenv("GROQ_MODEL_VISION") or os.getenv("GROQ_VISION_MODEL") or (os.getenv("GROQ_MODEL_TEXT") if "vision" in (os.getenv("GROQ_MODEL_TEXT","")) else None) or "llama-3.2-11b-vision-preview"
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
        log.info("[phish-groq] enable=%s model=%s guards=%s", ENABLE, MODEL_VISION, sorted(GUARD_IDS))

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if not ENABLE or not GROQ_KEY: return
        try:
            if message.author.bot: return
            ch = getattr(message,"channel",None)
            if not ch: return
            cid = int(getattr(ch,"id",0) or 0)
            pid = int(getattr(ch,"parent_id",0) or 0)
            if not ((cid in GUARD_IDS) or (pid and pid in GUARD_IDS)): return

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
            prompt = "Classify if this image is a phishing/login/QR/crypto-casino bait. Reply with JSON: {\"phish\":true/false, \"reason\":\"...\"}."
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
            # naive parse
            txt = ""
            try:
                txt = (data["choices"][0]["message"]["content"] or "").strip()
            except Exception:
                pass
            is_phish = ("\"phish\":true" in txt.lower()) or ("phish: true" in txt.lower())
            reason = txt[:180]
            if LOG_CHAN_ID:
                try:
                    logch = self.bot.get_channel(LOG_CHAN_ID) or await self.bot.fetch_channel(LOG_CHAN_ID)
                    if logch:
                        await logch.send(f"[phish-groq] sus image from <@{message.author.id}> → phish={is_phish} reason={reason}")
                except Exception:
                    pass
            log.info("[phish-groq] result=%s reason=%s", is_phish, reason)
            try:
                if is_phish:
                    ev_urls = [getattr(att,'url',None)] if getattr(att,'url',None) else []
                    emit_phish_detected(self.bot, message, {'score':1.0,'provider':'groq','reason':reason,'kind':'image'}, ev_urls)
            except Exception:
                pass
        except Exception as e:
            log.debug("[phish-groq] err: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(GroqPhishGuard(bot))
