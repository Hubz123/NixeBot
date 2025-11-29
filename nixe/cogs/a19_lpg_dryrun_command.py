from __future__ import annotations

"""
[a19-lpg-dryrun]
Admin-only dry-run LPG classification. No delete/ban.
Usage: /lpg_dryrun with image attachment.
"""

import logging
from typing import Optional

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

async def _read_attachment(att: discord.Attachment, limit_bytes: int = 8_000_000) -> Optional[bytes]:
    try:
        if getattr(att, "size", 0) and att.size > limit_bytes:
            return None
        return await att.read()
    except Exception:
        return None

class LPGDryRun(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="lpg_dryrun",
        description="Dry-run LPG classify for attached image (admin only).",
        with_app_command=True,
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def lpg_dryrun(self, ctx: commands.Context, image: Optional[discord.Attachment] = None):
        if image is None and ctx.message and ctx.message.attachments:
            image = ctx.message.attachments[0]
        if image is None:
            await ctx.reply("Attach an image to dry-run.")
            return

        data = await _read_attachment(image)
        if not data:
            await ctx.reply("Failed to read image or too large.")
            return

        try:
            import nixe.helpers.gemini_bridge as gb
        except Exception as e:
            await ctx.reply(f"gemini_bridge import failed: {e}")
            return

        try:
            res = await gb.classify_lucky_pull_bytes(data)
        except Exception as e:
            await ctx.reply(f"classify error: {e}")
            return

        lucky = bool(res.get("lucky", False))
        score = float(res.get("score", 0.0) or 0.0)
        reason = str(res.get("reason", ""))[:900]
        via = str(res.get("tag") or res.get("via") or "gemini")

        emb = discord.Embed(title="LPG Dry-Run Result", color=discord.Color.blurple())
        emb.add_field(name="Lucky", value=str(lucky), inline=True)
        emb.add_field(name="Score", value=f"{score:.3f}", inline=True)
        emb.add_field(name="Via", value=via, inline=True)
        emb.add_field(name="Reason", value=reason or "-", inline=False)
        emb.set_footer(text="Dry-run only. No actions taken.")
        await ctx.reply(embed=emb)

async def setup(bot):
    await bot.add_cog(LPGDryRun(bot))
