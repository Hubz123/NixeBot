
from __future__ import annotations
import io, discord
from discord.ext import commands
from nixe.helpers.env_reader import get, get_int
from nixe.helpers.lp_gemini_helper import is_gemini_enabled, is_lucky_pull
from nixe.helpers.safe_delete import safe_delete
def _csv_ids(s:str):
    return {int(x) for x in (s or "").replace(","," ").split() if x.isdigit()}
class LuckyPullGuard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot=bot
        self.enabled = get("LUCKYPULL_ENABLE","1")=="1"
        self.guard=_csv_ids(get("LUCKYPULL_GUARD_CHANNELS",""))
        self.allow=_csv_ids(get("LUCKYPULL_ALLOW_CHANNELS",""))
        self.redirect=get_int("LUCKYPULL_REDIRECT_CHANNEL_ID",0)
        self.th=float(get("GROQ_LUCKY_THRESHOLD","0.65"))
        self.use_gem=is_gemini_enabled()
    def _in_scope(self,ch:int)->bool:
        if self.allow and ch in self.allow: return False
        return (not self.guard) or (ch in self.guard)
    @commands.Cog.listener()
    async def on_message(self, m: discord.Message):
        if not self.enabled or m.author.bot or not hasattr(m.channel,"id") or not self._in_scope(m.channel.id): return
        if not (self.use_gem and m.attachments): return
        for a in m.attachments:
            name=(a.filename or "").lower()
            if not any(name.endswith(ext) for ext in (".png",".jpg",".jpeg",".webp")): continue
            b=await a.read()
            dec,score,_=is_lucky_pull(b, threshold=self.th)
            if dec:
                try: await safe_delete(m, label="delete", reason=str("Nixe: Lucky pull not allowed here"))
                except Exception: pass
                if self.redirect:
                    try:
                        ch=await self.bot.fetch_channel(self.redirect)
                        await ch.send(content=f"Dipindah dari {m.channel.mention} (lucky pull).", file=discord.File(io.BytesIO(b), filename=name or "image.png"))
                    except Exception: pass
                break
async def setup(bot: commands.Bot):
    await bot.add_cog(LuckyPullGuard(bot))
