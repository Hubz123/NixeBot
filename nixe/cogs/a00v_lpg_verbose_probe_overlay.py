# -*- coding: utf-8 -*-
import os, logging, discord
from discord.ext import commands
log = logging.getLogger("nixe.cogs.a00v_lpg_verbose_probe_overlay")

_IMAGE_EXTS = {".png",".jpg",".jpeg",".webp",".gif",".bmp",".tiff"}
def _cid(ch): 
    try: return int(getattr(ch, "id", 0) or 0)
    except Exception: return 0
def _pid(ch):
    try: return int(getattr(ch, "parent_id", 0) or 0)
    except Exception: return 0
def _parse_ids(val: str) -> set[int]:
    out = set()
    for part in str(val or "").replace(";",",").split(","):
        s = part.strip()
        if not s: continue
        try: out.add(int(s))
        except Exception: pass
    return out
def _is_image(att: discord.Attachment) -> bool:
    try:
        ct = (att.content_type or "").lower()
        if ct.startswith("image/"): return True
    except Exception: pass
    try:
        name = (att.filename or "").lower()
        for ext in _IMAGE_EXTS:
            if name.endswith(ext): return True
    except Exception: pass
    return False

class LPGVerboseProbe(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enable = (os.getenv("LPG_VERBOSE_PROBE","1") != "0")
        self.guard_ids = _parse_ids(os.getenv("LPG_GUARD_CHANNELS","") or os.getenv("LUCKYPULL_GUARD_CHANNELS",""))
        self.redirect_id = int(os.getenv("LPG_REDIRECT_CHANNEL_ID") or os.getenv("LUCKYPULL_REDIRECT_CHANNEL_ID") or "0")
        self.enabled_guard = (os.getenv("LPG_BRIDGE_ENABLE","1") == "1")
        self.require_classify = (os.getenv("LPG_REQUIRE_CLASSIFY") or "1") == "1"
        self.strict  = (os.getenv("LPG_STRICT_ON_GUARD") or os.getenv("LUCKYPULL_STRICT_ON_GUARD") or os.getenv("STRICT_ON_GUARD") or "1") == "1"
        self.thr = float(os.getenv("LPG_GEMINI_THRESHOLD") or os.getenv("GEMINI_LUCKY_THRESHOLD","0.85"))
        self.persona_enable = (os.getenv("LPG_PERSONA_ENABLE") or os.getenv("PERSONA_ENABLE") or "1") == "1"
        log.info("[lpg-probe] ready enable=%s guard_ids=%s redirect=%s require_classify=%s thr=%.2f strict=%s persona=%s",
                 self.enable, sorted(self.guard_ids), self.redirect_id, self.require_classify, self.thr, self.strict, self.persona_enable)
    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if not self.enable: return
        try:
            if not self.enabled_guard:
                log.info("[lpg-probe] skip: guard disabled (LPG_BRIDGE_ENABLE=0)"); return
            if not message or (getattr(message,"author",None) and message.author.bot): return
            ch = getattr(message,"channel",None)
            if not ch: return
            cid = _cid(ch); pid = _pid(ch)
            in_guard = (cid in self.guard_ids) or (pid and pid in self.guard_ids)
            if not in_guard:
                log.info("[lpg-probe] skip: not in guard (cid=%s pid=%s guard_ids=%s)", cid, pid, sorted(self.guard_ids)); return
            atts = list(message.attachments or [])
            img_count = sum(1 for a in atts if _is_image(a))
            if img_count == 0:
                log.info("[lpg-probe] skip: no image (attachments=%s names=%s)", len(atts), [getattr(a,'filename',None) for a in atts]); return
            log.info("[lpg-probe] pass: in_guard image_ok require_classify=%s (cid=%s pid=%s imgs=%s)", self.require_classify, cid, pid, img_count)
        except Exception as e:
            log.debug("[lpg-probe] error: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(LPGVerboseProbe(bot))
