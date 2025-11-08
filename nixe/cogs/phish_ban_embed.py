# -*- coding: utf-8 -*-
from __future__ import annotations
import os, logging, asyncio, datetime
import discord
from discord.ext import commands
from nixe.helpers import banlog

log = logging.getLogger("nixe.cogs.phish_ban_embed")

EMBED_COLOR = int(os.getenv("PHISH_EMBED_COLOR", "16007990"))  # default orange 0xF4511E
DELETE_AFTER_SECONDS = int(os.getenv("PHISH_EMBED_TTL", os.getenv("BAN_EMBED_TTL_SEC", "3600")))
AUTO_BAN = (os.getenv("PHISH_AUTO_BAN","0") == "1" or os.getenv("PHISH_AUTOBAN","0") == "1")
DELETE_MESSAGE = (os.getenv("PHISH_DELETE_MESSAGE","1") == "1")

class PhishBanEmbed(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        log.info("[phish-ban-embed] ready auto_ban=%s delete_message=%s", AUTO_BAN, DELETE_MESSAGE)

    @commands.Cog.listener("on_nixe_phish_detected")
    async def on_nixe_phish_detected(self, payload: dict):
        try:
            gid = payload.get("guild_id")
            mid = payload.get("message_id")
            cid = payload.get("channel_id")
            uid = payload.get("user_id")
            provider = payload.get("provider") or "groq"
            score = float(payload.get("score") or 0.0)
            reason = str(payload.get("reason") or "")
            evidence = payload.get("evidence") or []

            guild = self.bot.get_guild(int(gid)) if gid else None
            channel = self.bot.get_channel(int(cid)) if cid else None
            user = guild.get_member(int(uid)) if (guild and uid) else None

            # Build embed
            title = "💀 Phishing Detected"
            em = discord.Embed(title=title, color=EMBED_COLOR, timestamp=discord.utils.utcnow())
            em.add_field(name="User", value=f"<@{uid}>" if uid else "-", inline=True)
            em.add_field(name="Provider", value=str(provider), inline=True)
            em.add_field(name="Score", value=f"{score:.2f}", inline=True)
            if reason:
                em.add_field(name="Reason", value=reason[:512], inline=False)
            if evidence:
                em.add_field(name="Evidence", value="\n".join(evidence[:5]), inline=False)
            if cid and mid:
                em.add_field(name="Message", value=f"https://discord.com/channels/{gid}/{cid}/{mid}", inline=False)

            # Send embed to ban log channel (or channel itself if not configured)
            target = None
            try:
                if guild:
                    target = await banlog.get_ban_log_channel(guild)
            except Exception:
                target = None
            if not target:
                target = channel

            if target:
                await target.send(embed=em, delete_after=DELETE_AFTER_SECONDS)

            # Auto delete offending message (best-effort, optional)
            # Resolve safe data thread (never delete here)
            SAFE_DATA_THREAD = 0
            try:
                SAFE_DATA_THREAD = int(
                    os.getenv("PHISH_DATA_THREAD_ID") or
                    os.getenv("NIXE_PHISH_DATA_THREAD_ID") or
                    os.getenv("PHASH_IMAGEPHISH_THREAD_ID") or "0"
                )
            except Exception:
                SAFE_DATA_THREAD = 0
            if DELETE_MESSAGE and channel and mid:
                try:
                    msg = await channel.fetch_message(int(mid))
                    await msg.delete()
                except Exception:
                    pass

            # Auto-ban (optional)
            if AUTO_BAN and guild and user:
                try:
                    await guild.ban(user, reason=f"Phishing detected: {reason[:140]}", delete_message_days=0)
                except Exception:
                    pass
        except Exception as e:
            log.debug("[phish-ban-embed] err: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(PhishBanEmbed(bot))
