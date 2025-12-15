
from __future__ import annotations
import os
import logging
import discord
from discord.ext import commands
from nixe.helpers.bootstate import wait_cogs_loaded

log = logging.getLogger("nixe.discord.handlers_crucial")


async def wire_handlers(bot: commands.Bot) -> None:
    """Load core extensions and autodiscovered cogs.

    This is invoked early by nixe.discord.shim_runner. If it is missing, the bot
    will start without any of the security cogs, causing attachments to "tembus".
    """
    # 1) Ensure this extension's Cog is loaded (safe if already loaded)
    try:
        await setup(bot)
    except Exception:
        pass

    # 2) Load the cog autodiscovery extension
    try:
        await bot.load_extension("nixe.cogs_loader")
    except Exception as e:
        log.error("wire_handlers: failed to load nixe.cogs_loader: %r", e)

def _user_tag(u: discord.ClientUser | discord.User | None) -> str:
    if not u:
        return "Nixe#0000"
    discr = getattr(u, "discriminator", None)
    if discr and discr != "0":
        return f"{getattr(u, 'name', 'Nixe')}#{discr}"
    return getattr(u, "name", "Nixe")

class NixeHandlersCrucial(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._printed = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._printed:
            return
        # Wait a moment until auto-discovered cogs are loaded, but don't block too long
        await wait_cogs_loaded(5.0)
        self._printed = True
        log.info("🧩 Cogs loaded (core + autodiscover).")
        u = self.bot.user
        log.info("✅ Bot berhasil login sebagai %s (ID: %s)", _user_tag(u), getattr(u, "id", "?"))
        mode = os.getenv("NIXE_MODE", os.getenv("MODE", "production"))
        log.info("🌐 Mode: %s", mode)

async def setup(bot: commands.Bot):
    if 'NixeHandlersCrucial' in bot.cogs:
        return
    await bot.add_cog(NixeHandlersCrucial(bot))
