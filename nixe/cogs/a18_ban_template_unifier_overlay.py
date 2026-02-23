
from __future__ import annotations
import os, logging, discord, aiohttp, io
from PIL import Image
from discord.ext import commands
from ..cogs.ban_embed import build_ban_embed, build_classification_embed, build_ban_evidence_payload, build_ban_evidence_file
from ..config_ids import LOG_BOTPHISHING, TESTBAN_CHANNEL_ID
log = logging.getLogger("nixe.cogs.ban_template_unifier")

def _pick_log_channel_id(guild: discord.Guild) -> int:
    for k in ("PHISH_LOG_CHANNEL_ID", "PHISH_LOG_CHAN_ID"):
        v = os.getenv(k)
        if v and str(v).isdigit(): return int(v)
    return int(LOG_BOTPHISHING or TESTBAN_CHANNEL_ID or 0)


async def _fetch_ss_file(image_url: str, *, filename: str, max_bytes: int = 2_000_000) -> discord.File | None:
    """Fetch and downscale evidence image for ban logs (Render-safe)."""
    try:
        if not image_url or not isinstance(image_url, str):
            return None
        if not (image_url.startswith("http://") or image_url.startswith("https://")):
            return None
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(image_url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.content.read(max_bytes + 1)
        if not data or len(data) > max_bytes:
            return None
        try:
            im = Image.open(io.BytesIO(data))
            im = im.convert("RGB")
            im.thumbnail((1024, 1024))
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=85, optimize=True)
            out.seek(0)
            return discord.File(fp=out, filename=filename)
        except Exception:
            return discord.File(fp=io.BytesIO(data), filename=filename)
    except Exception:
        return None

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

        # Prepare SS (re-upload) if we have an evidence image URL.
        ss_file = None
        image_name = None
        if isinstance(ev, dict):
            img_url = str(ev.get("image_url") or "")
            if img_url:
                image_name = f"ban_ss_{int(getattr(user,'id',0) or 0)}.jpg"
                ss_file = await _fetch_ss_file(img_url, filename=image_name)

        # Prefer classification-style evidence embed (similar to LPG cards).
        if isinstance(ev, dict) and (ev.get("kind") in ("phish", "nsfw_invite") or (ev.get("provider") and (ev.get("urls") or ev.get("attachments") or ev.get("embeds")))):
            title = "Phishing Classification" if ev.get("kind") == "phish" else ("NSFW Invite Classification" if ev.get("kind") == "nsfw_invite" else "Ban Classification")
            channel_disp = ""
            try:
                cid2 = int(ev.get("channel_id") or 0)
                if cid2:
                    ch_obj2 = guild.get_channel(cid2) or guild.get_thread(cid2)
                    if ch_obj2:
                        channel_disp = f"#{getattr(ch_obj2,'name','')}"
                    else:
                        channel_disp = f"`{cid2}`"
            except Exception:
                channel_disp = ""

            extra = []
            for u0 in (ev.get("urls") or []):
                extra.append(u0)
            for a0 in (ev.get("attachments") or []):
                extra.append(a0)
            for e0 in (ev.get("embeds") or []):
                extra.append(e0)

            embed = build_classification_embed(
                title=title,
                result="BAN ✅",
                score=ev.get("score"),
                provider=str(ev.get("provider") or "—"),
                reason=str(ev.get("reason") or reason or "—"),
                message_id=int(ev.get("message_id") or 0),
                channel_display=channel_disp,
                jump_url=str(ev.get("jump_url") or ""),
                image_attachment_name=image_name if ss_file else None,
                extra_lines=extra[:8],
            )
        else:
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
                await ch.send(embed=embed, files=[x for x in (f, ss_file) if x])
        except Exception as e:
            log.warning("send ban embed failed: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(BanTemplateUnifier(bot))
