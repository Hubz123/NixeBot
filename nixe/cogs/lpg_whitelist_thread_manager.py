# nixe/cogs/lpg_whitelist_thread_manager.py
from __future__ import annotations
import logging, os, json
import discord
from discord.ext import commands

log = logging.getLogger("nixe.cogs.lpg_whitelist_thread_manager")

def _cfg() -> dict:
    path = os.getenv("RUNTIME_ENV_PATH") or "nixe/config/runtime_env.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _first_int(*vals) -> int:
    for v in vals:
        if v is None: 
            continue
        s = str(v).strip()
        if not s:
            continue
        try:
            return int(s)
        except Exception:
            continue
    return 0

class WhitelistThreadManager(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cfg = _cfg()
        self.thread_name = os.getenv("LPG_WHITELIST_THREAD_NAME", "lpg-whitelist")
        self.parent_chan_id = _first_int(
            os.getenv("LPG_WHITELIST_PARENT_CHANNEL_ID"),
            self.cfg.get("LPG_WHITELIST_PARENT_CHANNEL_ID"),
            os.getenv("LPG_NEG_PARENT_CHANNEL_ID"),
            self.cfg.get("LPG_NEG_PARENT_CHANNEL_ID"),
            os.getenv("LPG_PARENT_CHANNEL_ID"),
            self.cfg.get("LPG_PARENT_CHANNEL_ID"),
        )
        # Seed thread_id from config, but allow runtime overlays to override via environment.
        env_tid = os.getenv("LPG_WHITELIST_THREAD_ID") or ""
        if env_tid.isdigit():
            self.thread_id = int(env_tid)
        else:
            self.thread_id = int(self.cfg.get("LPG_WHITELIST_THREAD_ID") or 0)

    async def _ensure_thread(self):
        if not self.parent_chan_id:
            log.warning("[lpg-wl] parent channel id missing")
            return
        # Respect explicit NO_NEW_THREADS flag to avoid creating extra threads if admin wants a fixed one.
        no_new = (os.getenv("LPG_WHITELIST_NO_NEW_THREADS") or "0").strip().lower() in ("1", "true", "yes", "on")
        try:
            parent = self.bot.get_channel(self.parent_chan_id) or await self.bot.fetch_channel(self.parent_chan_id)
        except Exception as e:
            log.warning("[lpg-wl] failed to resolve parent channel: %r", e)
            return
        # Fast path: if we already have a thread id and it exists, done.
        if self.thread_id:
            try:
                th = await self.bot.fetch_channel(self.thread_id)
                if isinstance(th, discord.Thread):
                    return
            except Exception:
                pass
        # Allow runtime override from environment (e.g. singleton overlay) if it changed after init.
        env_tid = os.getenv("LPG_WHITELIST_THREAD_ID") or ""
        if env_tid.isdigit():
            try:
                env_tid_int = int(env_tid)
                th = await self.bot.fetch_channel(env_tid_int)
                if isinstance(th, discord.Thread):
                    self.thread_id = env_tid_int
                    return
            except Exception:
                pass
        # Otherwise locate by name under the parent channel.
        try:
            # search existing
            for th in parent.threads if hasattr(parent, "threads") else []:
                if th.name == self.thread_name:
                    self.thread_id = th.id
                    log.info("[lpg-wl] found existing thread id=%s name=%s", th.id, th.name)
                    return
            # create new if allowed
            if no_new:
                log.warning(
                    "[lpg-wl] whitelist thread with name=%r not found under parent=%s and NO_NEW_THREADS=1; not creating a new one",
                    self.thread_name,
                    parent.id,
                )
                return
            th = await parent.create_thread(name=self.thread_name, auto_archive_duration=10080)
            self.thread_id = th.id
            log.warning("[lpg-wl] created whitelist thread id=%s name=%s under parent=%s", th.id, th.name, parent.id)
        except Exception as e:
            log.exception("[lpg-wl] failed to create thread: %r", e)
    @commands.Cog.listener("on_ready")
    async def on_ready(self):
        try:
            await self._ensure_thread()
        except Exception:
            log.exception("[lpg-wl] ensure thread failed at on_ready")

async def setup(bot: commands.Bot):
    await bot.add_cog(WhitelistThreadManager(bot))
