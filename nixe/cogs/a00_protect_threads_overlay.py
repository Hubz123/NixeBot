# -*- coding: utf-8 -*-
from __future__ import annotations
import os, logging, discord
from discord.ext import commands
log = logging.getLogger("nixe.cogs.a00_protect_threads_overlay")
def _parse_ids(s):
    out=set()
    if not s: return out
    for p in s.split(","):
        p=p.strip()
        if not p: continue
        try: out.add(int(p))
        except: pass
    return out
PROTECT_IDS = _parse_ids(os.getenv("PROTECT_CHANNEL_IDS",""))

ALLOW_DELETE_IDS = _parse_ids(os.getenv("PROTECT_ALLOW_DELETE_IDS",""))
try:
    # Allow known housekeeping threads even if their parent is protected.
    wt = int(os.getenv("NIXE_YT_WUWA_WATCHLIST_THREAD_ID","0") or 0)
    if wt: ALLOW_DELETE_IDS.add(wt)
except Exception:
    pass
try:
    mt = int(os.getenv("LPG_STATUS_THREAD_ID","0") or 0)
    if mt: ALLOW_DELETE_IDS.add(mt)
except Exception:
    pass
# Hardcoded LPG memory thread (safety)
ALLOW_DELETE_IDS.add(1435924665615908965)
_ORIG_MSG_DELETE = discord.Message.delete
_ORIG_PURGE = getattr(discord.abc.Messageable,'purge',None)
async def _guarded_delete(self,*,delay=None):
    try:
        ch=self.channel
        if getattr(ch,'id',None) in ALLOW_DELETE_IDS:
            return await _ORIG_MSG_DELETE(self, delay=delay)
        if getattr(ch,'id',None) in PROTECT_IDS or getattr(ch,'parent_id',None) in PROTECT_IDS:
            log.warning("[protect] blocked delete in %s (msg=%s)", getattr(ch,'id','?'), self.id); return
    except: pass
    return await _ORIG_MSG_DELETE(self, delay=delay)
async def _guarded_purge(self,*a,**kw):
    try:
        if getattr(self,'id',None) in ALLOW_DELETE_IDS:
            return await _ORIG_PURGE(self,*a,**kw)
        if getattr(self,'id',None) in PROTECT_IDS or getattr(self,'parent_id',None) in PROTECT_IDS:
            log.warning("[protect] blocked purge in %s", getattr(self,'id','?')); return []
    except: pass
    return await _ORIG_PURGE(self,*a,**kw)
class ProtectThreadsOverlay(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        discord.Message.delete=_guarded_delete
        if _ORIG_PURGE: discord.abc.Messageable.purge=_guarded_purge
        if PROTECT_IDS: log.warning("[protect] active for IDs: %s", ",".join(map(str,sorted(PROTECT_IDS))))
async def setup(bot): await bot.add_cog(ProtectThreadsOverlay(bot))