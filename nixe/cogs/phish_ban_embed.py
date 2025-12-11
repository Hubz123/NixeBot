# -*- coding: utf-8 -*-
from __future__ import annotations

import os, logging
import discord
from discord.ext import commands

from nixe.helpers import banlog

log = logging.getLogger("nixe.cogs.phish_ban_embed")

EMBED_COLOR = int(os.getenv("PHISH_EMBED_COLOR", "16007990"))  # default orange 0xF4511E
DELETE_AFTER_SECONDS = int(os.getenv("PHISH_EMBED_TTL", os.getenv("BAN_EMBED_TTL_SEC", "3600")))
AUTO_BAN = (os.getenv("PHISH_AUTO_BAN", "0") == "1" or os.getenv("PHISH_AUTOBAN", "0") == "1")
DELETE_MESSAGE = (os.getenv("PHISH_DELETE_MESSAGE", "1") == "1")

# When BAN_UNIFIER_ENABLE=1 we normally let BanTemplateUnifier handle the
# pretty external-style embed and suppress this technical embed to avoid
# duplicate messages. PHISH_EMBED_FORCE=1 can override this behaviour.
DISABLE_SELF_EMBED = False



class PhishBanEmbed(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        log.info(
            "[phish-ban-embed] ready auto_ban=%s delete_message=%s disable_self_embed=%s",
            AUTO_BAN,
            DELETE_MESSAGE,
            DISABLE_SELF_EMBED,
        )

    @commands.Cog.listener("on_nixe_phish_detected")
    async def on_nixe_phish_detected(self, payload: dict) -> None:
        """Handle internal phishing detection events.

        Payload keys (best-effort, all optional):
        - guild_id, channel_id, message_id, user_id
        - provider, score, reason
        - evidence: list[str] of attachment names / URLs
        """
        try:
            gid = payload.get("guild_id")
            mid = payload.get("message_id")
            cid = payload.get("channel_id")
            uid = payload.get("user_id")
            provider = payload.get("provider") or "phash"
            try:
                score = float(payload.get("score") or 0.0)
            except Exception:
                score = 0.0
            reason = str(payload.get("reason") or "")
            evidence = payload.get("evidence") or []

            guild = self.bot.get_guild(int(gid)) if gid else None
            channel = self.bot.get_channel(int(cid)) if cid else None

            user = None
            if guild and uid:
                try:
                    user = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                except Exception:
                    user = None

            # Optional technical embed just for phishing log channel
            if not DISABLE_SELF_EMBED:
                title = "💀 Phishing Detected"
                em = discord.Embed(
                    title=title,
                    color=EMBED_COLOR,
                    timestamp=discord.utils.utcnow(),
                )
                em.add_field(
                    name="User",
                    value=f"<@{uid}>" if uid else "-",
                    inline=True,
                )
                em.add_field(
                    name="Provider",
                    value=str(provider),
                    inline=True,
                )
                em.add_field(
                    name="Score",
                    value=f"{score:.2f}",
                    inline=True,
                )
                if reason:
                    em.add_field(
                        name="Reason",
                        value=reason[:512],
                        inline=False,
                    )
                if evidence:
                    ev_lines = [str(x) for x in evidence[:5]]
                    em.add_field(
                        name="Evidence",
                        value="\n".join(ev_lines),
                        inline=False,
                    )
                if gid and cid and mid:
                    em.add_field(
                        name="Message",
                        value=f"https://discord.com/channels/{gid}/{cid}/{mid}",
                        inline=False,
                    )

                # Send embed to the original channel first; if unavailable, fall back to ban-log channel
                target = channel
                if not target and guild:
                    try:
                        target = await banlog.get_ban_log_channel(guild)
                    except Exception:
                        target = None

                if target:
                    try:
                        await target.send(embed=em, delete_after=DELETE_AFTER_SECONDS)
                    except Exception:
                        # Logging not critical – continue with delete/ban path
                        pass

            # Auto delete offending message (best-effort, optional)
            # Resolve safe data thread (never delete the mirror/data thread)
            safe_data_thread = 0
            try:
                safe_data_thread = int(
                    os.getenv("PHISH_DATA_THREAD_ID")
                    or os.getenv("NIXE_PHISH_DATA_THREAD_ID")
                    or os.getenv("PHASH_IMAGEPHISH_THREAD_ID")
                    or "0"
                )
            except Exception:
                safe_data_thread = 0

            if DELETE_MESSAGE and channel and mid:
                try:
                    if not safe_data_thread or int(channel.id) != safe_data_thread:
                        msg = await channel.fetch_message(int(mid))
                        await msg.delete()
                except Exception:
                    pass

            # Auto-ban (optional)
            if AUTO_BAN and guild and user:
                try:
                    await guild.ban(
                        user,
                        reason=f"Phishing detected: {reason[:140]}",
                        delete_message_days=0,
                    )
                except Exception:
                    pass
        except Exception as e:
            log.debug("[phish-ban-embed] err: %r", e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PhishBanEmbed(bot))
