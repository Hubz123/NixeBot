# nixe/cogs/a00_disable_phish_providers_overlay.py
import inspect
from discord.ext import commands
import os
KEEP_GROQ = (os.getenv('PHISH_GROQ_ENABLE','1') == '1')
CANDIDATES=[
 "nixe.cogs.image_phish_gemini_guard","nixe.cogs.phish_gemini_guard","nixe.cogs.phish_groq_guard","nixe.cogs.gemini_phish_guard"]
class DisablePhishProviders(commands.Cog):
    def __init__(self,bot): self.bot=bot
    async def _safe_unload(self,name:str):
        try:
            if name not in getattr(self.bot,"extensions",{}): return
            fn=getattr(self.bot,"unload_extension",None)
            if fn is None: return
            if inspect.iscoroutinefunction(fn): await fn(name)
            else: fn(name)
        except Exception: pass
    @commands.Cog.listener()
    async def on_ready(self):
        for ext in CANDIDATES:
            if KEEP_GROQ and ('phish_groq_guard' in ext):
                continue
            await self._safe_unload(ext)
async def setup(bot: commands.Bot): await bot.add_cog(DisablePhishProviders(bot))
