# -*- coding: utf-8 -*-
from __future__ import annotations
import logging
from discord.ext import commands

log = logging.getLogger(__name__)

class GuardStatus(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @commands.command(name='lpg_status', help='Show Lucky Pull Guard status.')
    @commands.has_permissions(manage_guild=True)
    async def lpg_status(self, ctx):
        cog = self.bot.get_cog('LuckyPullGuard')
        if not cog:
            await ctx.reply('LuckyPullGuard not loaded.', mention_author=False); return
        src = []
        try:
            from nixe.cogs.lucky_pull_guard import _cfg_origin
            src = [
                ('del_thr', _cfg_origin('LUCKYPULL_DELETE_THRESHOLD')),
                ('redir_thr', _cfg_origin('LUCKYPULL_REDIRECT_THRESHOLD')),
                ('wait', _cfg_origin('LUCKYPULL_MAX_LATENCY_MS')),
                ('strict', _cfg_origin('LUCKYPULL_DELETE_ON_GUARD')),
                ('groq_thr', _cfg_origin('GEMINI_LUCKY_THRESHOLD')),
            ]
        except Exception:
            pass
        txt = (f"guard_channels={sorted(getattr(cog,'guard_channels',[]))}\n"
               f"redirect={getattr(cog,'redirect_channel',None)}\n"
               f"delete>={getattr(cog,'min_conf_delete',0):.2f} redirect>={getattr(cog,'min_conf_redirect',0):.2f} groq_thr={getattr(cog,'groq_lucky_thr',0):.2f}\n"
               f"strict_on_guard={getattr(cog,'strict_delete_on_guard',False)} wait_ms={getattr(cog,'wait_ms',0)}\n"
               f"envsrc={src}")
        await ctx.reply(f"```\n{txt}\n```", mention_author=False)

async def setup(bot):
    if bot.get_cog('GuardStatus'): return
    try:
        await bot.add_cog(GuardStatus(bot))
    except Exception:
        pass
