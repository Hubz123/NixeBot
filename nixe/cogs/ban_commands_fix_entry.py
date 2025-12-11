from __future__ import annotations
from discord.ext import commands
from ._fix.ban_commands_fix import BanCommandsFix

async def setup(bot: commands.Bot):
    # Thin wrapper so cogs_loader (which skips names starting with '_')
    # still loads BanCommandsFix via this public entry module.
    await bot.add_cog(BanCommandsFix(bot))
