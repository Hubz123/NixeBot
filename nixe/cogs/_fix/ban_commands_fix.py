from __future__ import annotations
import re
import discord
from discord.ext import commands

class BanCommandsFix(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Fallback ban command (prefix-based)
    @commands.command(name="banfix")
    @commands.has_permissions(ban_members=True)
    async def banfix(self, ctx: commands.Context, user: discord.User, *, reason: str = "banfix"):
        try:
            await ctx.guild.ban(user, reason=reason)
            await ctx.reply(f"✅ Banned {user} (fallback).")
        except Exception as e:
            await ctx.reply(f"❌ Ban failed: {e}")

    # Fallback unban command (prefix-based)
    @commands.command(name="unbanfix")
    @commands.has_permissions(ban_members=True)
    async def unbanfix(self, ctx: commands.Context, user_id: int):
        try:
            await ctx.guild.unban(discord.Object(id=user_id))
            await ctx.reply(f"✅ Unbanned {user_id} (fallback).")
        except Exception as e:
            await ctx.reply(f"❌ Unban failed: {e}")

    # Special &unban command usable only by moderators (ban_members permission)
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore DMs and bot messages
        if message.author.bot or message.guild is None:
            return

        content = message.content.strip()
        if not content.startswith("&"):
            return

        # Only handle &unban ... pattern
        if not content.lower().startswith("&unban"):
            return

        # Check permission: only members with ban_members can use this
        perms = message.channel.permissions_for(message.author)
        if not perms.ban_members:
            # Silently ignore for non-mods
            return

        parts = content.split(maxsplit=1)
        if len(parts) < 2:
            await message.channel.send(
                "Usage: `&unban <user_id>`",
                delete_after=15,
            )
            return

        raw_id = parts[1].strip()
        # Allow plain ID or mention
        cleaned = re.sub(r"[^0-9]", "", raw_id)
        try:
            user_id = int(cleaned)
        except Exception:
            await message.channel.send(
                "User ID tidak valid. Contoh: `&unban 123456789012345678`",
                delete_after=15,
            )
            return

        try:
            await message.guild.unban(
                discord.Object(id=user_id),
                reason=f"&unban by {message.author} ({message.author.id})",
            )
            await message.channel.send(
                f"✅ Unbanned `{user_id}` via &unban.",
                reference=message,
                mention_author=False,
            )
        except Exception as e:
            await message.channel.send(
                f"❌ Unban gagal: `{e}`",
                reference=message,
                mention_author=False,
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(BanCommandsFix(bot))
