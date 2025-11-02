# -*- coding: utf-8 -*-
"""
nixe.cogs.persona_admin
Safe replacement: loads cleanly, no external deps, no yandere.json edits.
Provides /persona_status (prefix cmd: persona_status) for quick check.
"""
from __future__ import annotations

import os
import logging
from typing import Optional

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

def _env(k: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(k, default)
    return str(v) if v is not None else None

class PersonaAdmin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.mode = _env("LPG_PERSONA_MODE", "yandere")
        self.tone = _env("LPG_PERSONA_TONE", "auto")
        self.persona_file = _env("LPG_PERSONA_FILE") or _env("PERSONA_FILE") or "nixe/config/yandere.json"
        log.info("[persona-admin] loaded mode=%s tone=%s file=%s", self.mode, self.tone, self.persona_file)

    @commands.hybrid_command(name="persona_status", description="Show current persona mode/tone (no edits)")
    @commands.guild_only()
    async def persona_status(self, ctx: commands.Context) -> None:
        embed = discord.Embed(title="Persona Status", color=0xFF66AA)
        embed.add_field(name="Mode", value=str(self.mode), inline=True)
        embed.add_field(name="Tone", value=str(self.tone), inline=True)
        embed.add_field(name="File", value=str(self.persona_file), inline=False)
        await ctx.reply(embed=embed, mention_author=False)

async def setup(bot: commands.Bot) -> None:
    # Some forks return a coroutine from add_cog; await if needed.
    result = bot.add_cog(PersonaAdmin(bot))
    try:
        import asyncio
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass
