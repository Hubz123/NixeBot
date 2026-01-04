
from __future__ import annotations
import re, io
import discord
from discord.ext import commands
from nixe.helpers.env_reader import get, get_int
from nixe.helpers.phash_tools import dhash_bytes, hamming
from nixe.helpers.phash_board import get_blacklist_hashes
URL_RE = re.compile(r"https?://[\w.-]+\.[a-z]{2,}(?:/\S*)?", re.I)
_PRESET_TEXT = {"suspicious":"Suspicious or spam account","compromised":"Compromised or hacked account","breaking":"Breaking server rules","other":"Other"}
def _ban_reason():
    preset = get("PHISH_BAN_PRESET","suspicious").lower().strip()
    base = _PRESET_TEXT.get(preset,_PRESET_TEXT["suspicious"])
    custom = get("PHISH_BAN_REASON","").strip()
    return f"{base} | {custom}" if preset=="other" and custom else base
def _delete_history_seconds():
    raw = get("PHISH_BAN_DELETE_HISTORY","7d").lower().strip()
    if raw in ("none","0","no","off"): return 0
    if raw.endswith("d"):
        try: return max(0,min(int(raw[:-1])*86400,604800))
        except: return 604800
    if raw.endswith("h"):
        try: return max(0,min(int(raw[:-1])*3600,604800))
        except: return 0
    try:
        v=int(raw); return max(0,min(v,604800))
    except: return 604800
class FirstTouchdownFirewall(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot=bot
        self.enabled = get("PHISH_FTF_ENABLE","1")=="1"
        self.guard = {int(x) for x in (get("PHISH_FTF_GUARD_CHANNELS","").replace(","," ").split()) if x.isdigit()}
        self.allow = {int(x) for x in (get("PHISH_FTF_ALLOW_CHANNELS","").replace(","," ").split()) if x.isdigit()}
        # Global skip channels (mods/safe); shared with other phish guards
        skip_raw = get("PHISH_SKIP_CHANNELS","").replace(","," ").split()
        self.skip = {int(x) for x in skip_raw if x.isdigit()}
        if not self.skip:
            # Default: mod channels that must never be auto-banned by FTF
            self.skip = {1400375184048787566, 936690788946030613}
        self.block = set(get("PHISH_BLOCK_DOMAINS","").lower().replace(","," ").split())
        self.hash_thr = int(get_int("PHISH_HASH_HAMMING_MAX",6))
        self.hash_ref = get_blacklist_hashes()
    def _in_scope(self, ch_id:int)->bool:
        if ch_id in self.skip: 
            return False
        if self.allow and ch_id in self.allow: 
            return False
        return (not self.guard) or (ch_id in self.guard)
    async def _banish(self, m: discord.Message, reason_suffix: str):
        reason=f"{_ban_reason()} • {reason_suffix}"
        secs=_delete_history_seconds()
        try:
            await m.guild.ban(m.author, reason=reason, delete_message_seconds=secs)
        except TypeError:
            days=min(7, secs//86400)
            await m.guild.ban(m.author, reason=reason, delete_message_days=days)
        except Exception:
            try: await m.delete(reason=reason)
            except Exception: pass
    def _link_hit(self, content:str)->bool:
        for match in URL_RE.finditer(content or ""):
            host = match.group(0).split("/")[2].lower()
            if any(b and b in host for b in self.block): return True
        return False
    async def _image_hit(self, m:discord.Message)->bool:
        if not self.hash_ref: return False
        for a in m.attachments:
            n=(a.filename or "").lower()
            if not any(n.endswith(ext) for ext in (".png",".jpg",".jpeg",".webp",".gif")): continue
            try: b=await a.read()
            except Exception: continue
            hv=dhash_bytes(b)
            if hv==0: continue
            for ref in self.hash_ref:
                if hamming(hv,ref)<=self.hash_thr: return True
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
        try:
            pid = int(getattr(ch, "parent_id", 0) or 0)
        except Exception:
            pid = 0
        # Global rule: never apply first-touchdown firewall inside threads (forum or text).
        if pid:
            return
        # Respect PHISH_SKIP_CHANNELS (mods/safe/boards/forums).
        if cid in self.skip or (pid and pid in self.skip):
            return
        if not self._in_scope(cid):
            return
        if self._link_hit(m.content):
            await self._banish(m, "phishing link")
            return
        if m.attachments and await self._image_hit(m):
            await self._banish(m, "phishing image")
            return
async def setup(bot: commands.Bot):
    await bot.add_cog(FirstTouchdownFirewall(bot))
