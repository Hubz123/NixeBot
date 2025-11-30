# -*- coding: utf-8 -*-
from __future__ import annotations
import logging, sys
from discord.ext import commands

log = logging.getLogger(__name__)

def _safe_print(msg: str):
    try:
        print(msg)
    except Exception:
        try:
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(encoding='utf-8')
            print(msg)
        except Exception:
            try:
                print(msg.encode('ascii','replace').decode('ascii'))
            except Exception:
                pass

class LiveBanner(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        # Live banner output disabled (was: '==> Your service is live 🎉')
        return

async def setup(bot):
    if bot.get_cog('LiveBanner'): return
    try:
        await bot.add_cog(LiveBanner(bot))
    except Exception:
        pass
