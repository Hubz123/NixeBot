# -*- coding: utf-8 -*-
from __future__ import annotations
import logging
import discord
from discord.ext import commands
from nixe.helpers.env_reader import get as _cfg_get, get_int as _cfg_int, get_bool01 as _cfg_bool01
from nixe.helpers.gemini_phish import classify_image_phish
log = logging.getLogger(__name__)
def _compress(raw: bytes, max_px=640, min_px=384, target_kb=300, quality=75):
    try:
        import io
        from PIL import Image  # type: ignore
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((max_px, max_px))
        buf=io.BytesIO(); im.save(buf, format="JPEG", optimize=True, quality=quality, subsampling=1)
        data=buf.getvalue(); kb=len(data)//1024
        while kb>target_kb and (im.size[0]>min_px or im.size[1]>min_px):
            im = im.resize((max(min_px,int(im.size[0]*0.875)), max(min_px,int(im.size[1]*0.875))))
            quality = max(55, quality-5)
            buf=io.BytesIO(); im.save(buf, format="JPEG", optimize=True, quality=quality, subsampling=1)
            data=buf.getvalue(); kb=len(data)//1024
        return data
    except Exception:
        return raw
class ImagePhishGeminiGuard(commands.Cog):
    def __init__(self, bot):
        self.bot=bot
        self.enabled = _cfg_bool01("PHISH_GEMINI_ENABLE","1")=="1"
        self.threshold = float(_cfg_get("PHISH_GEMINI_THRESHOLD","0.92"))
        self.timeout_ms = _cfg_int("PHISH_GEMINI_MAX_LATENCY_MS", 9000)
        self.max_imgs = max(1, _cfg_int("PHISH_GEMINI_MAX_IMAGES", 2))
        log.info("[phish-gemini] enabled=%s thr=%.2f", self.enabled, self.threshold)
    async def _handle(self, msg: discord.Message):
        if not self.enabled or msg.author.bot: return
        imgs=[a for a in msg.attachments if a.content_type and a.content_type.startswith("image/")]
        if not imgs: return
        datas=[]
        for a in imgs[:self.max_imgs]:
            try: datas.append(_compress(await a.read()))
            except Exception: pass
        if not datas: return
        label, conf = await classify_image_phish(datas, hints="discord scam check", timeout_ms=self.timeout_ms)
        if label=="phish" and conf>=self.threshold:
            try: await msg.delete(reason=f"image phishing (gemini conf={conf:.2f})")
            except discord.Forbidden: log.warning("[phish-gemini] missing Manage Messages")
            except Exception as e: log.warning("[phish-gemini] delete failed: %r", e)
            try: await msg.channel.send(f"{msg.author.mention} gambar kamu terindikasi scam/penipuan. Mohon hindari upload konten itu. (conf={conf:.2f})", delete_after=20)
            except Exception: pass
    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        try: await self._handle(msg)
        except Exception as e: log.warning("[phish-gemini] err: %r", e)
async def setup(bot): 
    try: await bot.add_cog(ImagePhishGeminiGuard(bot)); log.info("[phish-gemini] loaded")
    except Exception as e: log.error("[phish-gemini] setup failed: %r", e)
