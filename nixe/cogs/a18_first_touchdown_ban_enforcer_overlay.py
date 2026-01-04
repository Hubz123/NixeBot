
from __future__ import annotations
import os, types, asyncio, logging
import discord
from discord.ext import commands
from ..cogs.ban_embed import build_ban_embed
from ..config_ids import LOG_BOTPHISHING, TESTBAN_CHANNEL_ID

log = logging.getLogger("nixe.cogs.first_touchdown_ban_enforcer")

def _env_int(name: str, default: int) -> int:
    try: return int(os.getenv(name, str(default)))
    except Exception: return default

def _pick_log_channel_id() -> int:
    for k in ("PHISH_LOG_CHANNEL_ID","PHISH_LOG_CHAN_ID"):
        v = os.getenv(k)
        if v and str(v).isdigit(): return int(v)
    return int(LOG_BOTPHISHING or TESTBAN_CHANNEL_ID or 0)

async def _patch_instance(bot: commands.Bot, cog: commands.Cog):
    reason_default = os.getenv("BAN_REASON", "Suspicious or spam account")
    delete_days   = _env_int("PHISH_DELETE_MESSAGE_DAYS", 7)
    ttl           = _env_int("BAN_EMBED_TTL_SEC", 15)

    if not hasattr(cog, "_ban_and_embed"): return

    async def _patched(self, m: discord.Message):
        try:
            await m.guild.ban(m.author, reason=reason_default, delete_message_days=max(0, min(7, delete_days)))
        except Exception as e:
            log.warning("ban failed: %r", e)
        moderator = None
        try:
            async for entry in m.guild.audit_logs(limit=4, action=discord.AuditLogAction.ban):
                if entry.target and int(getattr(entry.target,"id",0) or 0) == int(getattr(m.author,"id",0) or 0):
                    moderator = entry.user
                    break
        except Exception: pass
        try:
            embed = build_ban_embed(target=m.author, moderator=moderator, reason=reason_default, evidence_url=None, simulate=False)
            cid = _pick_log_channel_id()
            if cid:
                ch = m.guild.get_channel(cid) or await bot.fetch_channel(cid)
                sent = await ch.send(embed=embed)
                if ttl > 0:
                    await asyncio.sleep(ttl)
                    try: await sent.delete()
                    except Exception: pass
        except Exception as e:
            log.warning("log embed send failed: %r", e)

    cog._ban_and_embed = types.MethodType(_patched, cog)  # type: ignore

class FirstTouchdownBanEnforcer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = bot.loop.create_task(self._install())

    async def _install(self):
        await asyncio.sleep(0.5)
        for name, cog in list(self.bot.cogs.items()):
            if hasattr(cog, "_ban_and_embed"):
                await _patch_instance(self.bot, cog)
                log.warning("[ban-enforcer] patched _ban_and_embed on cog '%s'", name)

async def setup(bot: commands.Bot):
    # Disabled: rely on original _ban_and_embed implementations (no extra ban-enforcer overlay)
    log.info("[ban-enforcer] disabled; using original _ban_and_embed behavior")
    return
