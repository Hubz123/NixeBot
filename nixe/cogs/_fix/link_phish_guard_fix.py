from __future__ import annotations
import re, logging, discord
from discord.ext import commands

log = logging.getLogger(__name__)

def _cfg(key, default=None):
    try:
        from nixe.config import load as _load_cfg  # type: ignore
        return (_load_cfg() or {}).get(key, default)
    except Exception:
        return default

def _patterns():
    pats = _cfg("URL_BAN_PATTERNS", []) or []
    out = []
    for p in pats:
        try:
            out.append(re.compile(p, re.I))
        except Exception:
            pass
    return out

class LinkPhishGuardFix(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.patterns = _patterns()

    @commands.Cog.listener("on_message")
    async def _check(self, message: discord.Message):
        if message.author.bot:
            return
        hay = message.content or ""
        try:
            hay += " " + " ".join([a.url for a in getattr(message, "attachments", []) if hasattr(a, "url")])
        except Exception:
            pass
        for rx in self.patterns:
            if rx.search(hay):
                try:
                    await message.delete()
                except Exception:
                    pass
                try:
                    await message.guild.ban(message.author, reason="phish link (fallback guard)")
                except Exception:
                    pass
                log.info("[fallback link_guard] acted in guild=%s", getattr(message.guild, "id", "?"))
                break

async def setup(bot: commands.Bot):
    await bot.add_cog(LinkPhishGuardFix(bot))
