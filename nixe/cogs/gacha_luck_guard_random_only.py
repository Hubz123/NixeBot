from __future__ import annotations
import json, pathlib, asyncio
from typing import Optional, List
import discord
from discord.ext import commands

from nixe.helpers.persona import yandere
from nixe.helpers.lucky_classifier import classify_image_meta

CFG_PATH = pathlib.Path(__file__).resolve().parents[1] / "config" / "gacha_guard.json"

def _load_cfg() -> dict:
    with CFG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)

class GachaLuckGuardRandomOnly(commands.Cog):
    """Delete/redirect ONLY if image is confidently detected as lucky pull.
    Otherwise ignore (never delete). Persona line chosen randomly (soft/agro/sharp).
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cfg = _load_cfg().get("lucky_guard", {})
        self.enable = bool(self.cfg.get("enable", True))
        self.guard_channels = set(int(x) for x in self.cfg.get("guard_channels", []))
        self.redirect_channel = int(self.cfg.get("redirect_channel", 0)) or None
        self.review_thread_id = self.cfg.get("review_thread_id")
        self.min_conf_delete = float(self.cfg.get("min_confidence_delete", 0.85))
        self.min_conf_redirect = float(self.cfg.get("min_confidence_redirect", 0.60))
        self.dm_user_on_delete = bool(self.cfg.get("dm_user_on_delete", False))

    @commands.Cog.listener("on_message")
    async def _on_message(self, msg: discord.Message):
        if not self.enable or msg.author.bot:
            return
        if not msg.attachments:
            return
        if self.guard_channels and (msg.channel.id not in self.guard_channels):
            return  # only guard configured channels

        # Only analyze image attachments
        images = [a for a in msg.attachments if (a.content_type or "").startswith("image/")]
        if not images:
            return

        # For each image, get conservative confidence
        best_conf = 0.0
        for a in images:
            meta = classify_image_meta(filename=a.filename)
            best_conf = max(best_conf, meta["confidence"])

        # Decision: NEVER delete unless >= min_conf_delete
        #  - if >= min_conf_delete: delete + persona reply + optional DM
        #  - elif >= min_conf_redirect and redirect_channel: forward without delete
        #  - else: ignore (do nothing) to prevent false positives
        try:
            if best_conf >= self.min_conf_delete:
                # delete & notify
                reason = "deteksi lucky pull (random mode)"
                user_mention = msg.author.mention
                channel_name = f"#{msg.channel.name}"
                line = yandere(user=user_mention, channel=channel_name, reason=reason)
                await msg.delete()
                await msg.channel.send(line, delete_after=10)
                if self.dm_user_on_delete:
                    try:
                        await msg.author.send(f"Kontenmu di {channel_name} dihapus: {reason}")
                    except Exception:
                        pass
                # redirect original images if redirect_channel set
                if self.redirect_channel:
                    try:
                        target = msg.guild.get_channel(self.redirect_channel) or await self.bot.fetch_channel(self.redirect_channel)
                        files = [await a.to_file() for a in images]
                        content = f"{user_mention} dipindah ke sini karena {reason}."
                        await target.send(content=content, files=files)
                    except Exception:
                        pass
                return

            if best_conf >= self.min_conf_redirect and self.redirect_channel:
                # forward only, do not delete
                try:
                    target = msg.guild.get_channel(self.redirect_channel) or await self.bot.fetch_channel(self.redirect_channel)
                    files = [await a.to_file() for a in images]
                    await target.send(content=f"{msg.author.mention} kontenmu dipindah (uncertain).", files=files)
                except Exception:
                    pass
                return

            # else: do nothing (prevent accidental deletion)
        except discord.Forbidden:
            # missing perms; fail silently
            return
        except Exception:
            return

async def setup(bot: commands.Bot):
    # modern discord.py uses async setup
    await bot.add_cog(GachaLuckGuardRandomOnly(bot))
