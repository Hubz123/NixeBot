
from __future__ import annotations
import os, logging, discord
from discord.ext import commands
from ..cogs.ban_embed import build_ban_embed, build_ban_evidence_payload, build_ban_evidence_file
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
        if os.getenv("BAN_UNIFIER_ENABLE","1") == "0":
            return
        reason = os.getenv("BAN_REASON", None); moderator = None
        try:
            async for entry in guild.audit_logs(limit=6, action=discord.AuditLogAction.ban):
                if entry.target and int(getattr(entry.target,"id",0) or 0) == int(user.id):
                    moderator = entry.user; reason = entry.reason or reason; break
        except Exception: pass
        # Pull any cached evidence for this user/guild (best-effort) and reuse it for both embed + attachment.
        ev = None
        try:
            from nixe.helpers import phish_evidence_cache as _pec
            ev = _pec.pop(int(getattr(guild,'id',0) or 0), int(getattr(user,'id',0) or 0))
        except Exception:
            ev = None
        embed = build_ban_embed(
            target=user,
            moderator=moderator,
            reason=reason,
            guild=guild,
            evidence_url=None,
            simulate=False,
            evidence=ev,
        )
        try:
            cid = _pick_log_channel_id(guild)
            if cid:
                ch = guild.get_channel(cid) or await self.bot.fetch_channel(cid)
                payload = build_ban_evidence_payload(guild=guild, target=user, moderator=moderator, reason=reason, evidence=ev)
                fn = f"ban_evidence_{int(getattr(user,'id',0) or 0)}.json"
                f = build_ban_evidence_file(payload, filename=fn)
                await ch.send(embed=embed, file=f)
        except Exception as e:
            log.warning("send ban embed failed: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(BanTemplateUnifier(bot))
