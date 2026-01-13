
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, json, aiohttp, logging
from typing import Optional
import discord
from discord.ext import commands

log = logging.getLogger(__name__)

from nixe.helpers.env_reader import get as _cfg_get
API_KEY = _cfg_get('TRANSLATE_GEMINI_API_KEY') or os.getenv("TRANSLATE_GEMINI_API_KEY")
DEFAULT_MODEL = _cfg_get('TRANSLATE_GEMINI_MODEL', 'gemini-2.5-flash')

class GeminiHealth(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    @commands.command(name="gemini-check")
    async def gemini_check(self, ctx: commands.Context, model: Optional[str]=None):
        if not API_KEY:
            await ctx.reply("Gemini translate API key belum di-set (TRANSLATE_GEMINI_API_KEY).", mention_author=False)
            return
        mdl = model or DEFAULT_MODEL
        base = "https://generativelanguage.googleapis.com/v1beta"
        # metadata
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(f"{base}/models/{mdl}?key={API_KEY}") as r:
                    meta = await r.json()
        except Exception as e:
            await ctx.reply(f"models/{mdl}: error {e!r}", mention_author=False)
            return

        body = {
            "contents": [{"role":"user","parts":[{"text":"Return ONLY: {\"ok\":true}"}]}],
            "generationConfig": {"responseMimeType": "application/json"}
        }
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(f"{base}/models/{mdl}:generateContent?key={API_KEY}", json=body) as r:
                    data = await r.json()
        except Exception as e:
            await ctx.reply(f"generateContent error: {e!r}", mention_author=False)
            return

        meta_name = meta.get("name", mdl)
        mdl_id = meta.get("baseModelId", mdl)
        await ctx.reply(f"gemini-check OK: `{meta_name}` (id=`{mdl_id}`)", mention_author=False)

async def setup(bot: commands.Bot):
    await bot.add_cog(GeminiHealth(bot))
