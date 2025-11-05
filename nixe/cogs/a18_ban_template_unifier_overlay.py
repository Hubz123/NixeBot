
from __future__ import annotations
import os, logging, discord
from discord.ext import commands
from ..cogs.ban_embed import build_ban_embed
from ..config_ids import LOG_BOTPHISHING, TESTBAN_CHANNEL_ID
log = logging.getLogger("nixe.cogs.ban_template_unifier")

def _pick_log_channel_id(guild: discord.Guild) -> int:
    for k in ("PHISH_LOG_CHANNEL_ID", "PHISH_LOG_CHAN_ID"):
        v = os.getenv(k)
        if v and str(v).isdigit(): return int(v)
    return int(LOG_BOTPHISHING or TESTBAN_CHANNEL_ID or 0)

class BanTemplateUnifier(commands.Cog):
    def __init__(self, bot: commands.Bot): self.bot = bot
    @commands.Cog.listener("on_member_ban")
    async def _on_member_ban(self, guild: discord.Guild, user: discord.User):
        if os.getenv("BAN_UNIFIER_ENABLE","0") != "1":
            return
        reason = os.getenv("BAN_REASON", None); moderator = None
        try:
            async for entry in guild.audit_logs(limit=6, action=discord.AuditLogAction.ban):
                if entry.target and int(getattr(entry.target,"id",0) or 0) == int(user.id):
                    moderator = entry.user; reason = entry.reason or reason; break
        except Exception: pass
        embed = build_ban_embed(target=user, moderator=moderator, reason=reason, evidence_url=None, simulate=False)
        try:
            cid = _pick_log_channel_id(guild)
            if cid:
                ch = guild.get_channel(cid) or await self.bot.fetch_channel(cid)
                await ch.send(embed=embed)
        except Exception as e:
            log.warning("send ban embed failed: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(BanTemplateUnifier(bot))
