
from __future__ import annotations
import logging
from typing import Optional, Union
import discord
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# Force WIB timestamp without relying on zoneinfo, so it always shows "WIB"
def _now_wib_str() -> str:
    try:
        # Use UTC+7 offset and label explicitly as WIB
        t = datetime.utcnow() + timedelta(hours=7)
        return t.strftime("%Y-%m-%d %H:%M:%S") + " WIB"
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " WIB"

def _safe_name(u: Union[discord.User, discord.Member, None]) -> str:
    if not u:
        return "Unknown"
    name = getattr(u, "name", None) or "Unknown"
    discr = getattr(u, "discriminator", None)
    if discr and discr != "0":
        return f"{name}#{discr}"
    return name

def _thumb_url(u: Union[discord.User, discord.Member, None]) -> Optional[str]:
    try:
        if u and u.display_avatar:
            return str(u.display_avatar.url)
    except Exception:
        return None
    return None

def build_ban_embed(
    target: Union[discord.Member, discord.User, None],
    moderator: Union[discord.Member, discord.User, None],
    reason: Optional[str] = None,
    *,
    simulate: Optional[bool] = None,
    dry_run: Optional[bool] = None,
    guild: Optional[discord.Guild] = None,
    **kwargs,
) -> discord.Embed:
    """
    Build embed that matches external style exactly for Test Ban / Ban.
    - Title: '💀 Test Ban (Simulasi)' when simulate/dry_run, else '⛔ Ban'.
    - Color: red accent (left border).
    - Fields: 'Target:', 'Moderator:', 'Reason:' (reason '—' when empty).
    - Description (simulate only): italic Indonesian sentence.
    - Thumbnail: target avatar if available.
    - Footer: 'external • <YYYY-MM-DD HH:MM:SS WIB>'.
    """
    is_sim = bool(simulate or dry_run)

    title = "💀 Test Ban (Simulasi)" if is_sim else "⛔ Ban"
    color = discord.Color.red()

    embed = discord.Embed(title=title, color=color)

    # Field values
    if target:
        mention = getattr(target, "mention", None) or f"`{_safe_name(target)}`"
        tid = getattr(target, "id", None)
        target_val = f"{mention} ({tid})" if tid else f"{mention}"
    else:
        target_val = "—"

    if moderator:
        mod_val = getattr(moderator, "mention", None) or f"`{_safe_name(moderator)}`"
    else:
        mod_val = "—"

    embed.add_field(name="Target:", value=target_val, inline=False)
    embed.add_field(name="Moderator:", value=mod_val, inline=False)
    embed.add_field(name="Reason:", value=(reason if (reason and str(reason).strip()) else "—"), inline=False)


    # Optional: attach human-readable phishing evidence (URL / attachment / embed) if available.
    # Best-effort; never raises and never affects ban flow.
    try:
        if guild and target and getattr(target, "id", None):
            from nixe.helpers import phish_evidence_cache as _pec
            ev = _pec.pop(int(getattr(guild, "id", 0) or 0), int(getattr(target, "id", 0) or 0))
            if ev:
                lines_ev = []
                j = str(ev.get("jump_url") or "").strip()
                if j:
                    lines_ev.append(f"Message: {j}")
                sn = str(ev.get("snippet") or "").strip()
                if sn:
                    # keep it short to fit embed limits
                    if len(sn) > 220:
                        sn = sn[:217] + "..."
                    lines_ev.append(f"Text: {sn}")
                # Primary samples: attachments / urls / embeds
                atts = ev.get("attachments") or []
                if isinstance(atts, list) and atts:
                    for a in atts[:3]:
                        try:
                            s = str(a).strip()
                            if not s:
                                continue
                            # accept "filename | url" or plain url
                            if "|" in s and "http" in s:
                                left, right = s.split("|", 1)
                                fn = left.strip()[:80] or "file"
                                url = right.strip()[:220]
                                if url:
                                    lines_ev.append(f"Attachment: {fn} | {url}")
                            else:
                                lines_ev.append(f"Attachment: {s[:220]}")
                        except Exception:
                            continue
                urls = ev.get("urls") or []
                if isinstance(urls, list) and urls:
                    for u in urls[:3]:
                        try:
                            u = str(u).strip()
                            if u:
                                lines_ev.append(f"URL: {u[:220]}")
                        except Exception:
                            continue
                ems = ev.get("embeds") or []
                if isinstance(ems, list) and ems:
                    for u in ems[:2]:
                        try:
                            u = str(u).strip()
                            if u:
                                lines_ev.append(f"Embed: {u[:220]}")
                        except Exception:
                            continue

                # Compose field value (Discord field value max 1024)
                val = "\n".join(lines_ev).strip()
                if val:
                    if len(val) > 1024:
                        val = val[:1021] + "..."
                    embed.add_field(name="Evidence:", value=val, inline=False)

                # If we have an image URL, show preview to make review faster.
                img = str(ev.get("image_url") or "").strip()
                if img:
                    try:
                        embed.set_image(url=img)
                    except Exception:
                        pass
    except Exception:
        pass

    if is_sim:
        embed.description = "*Ini hanya simulasi. Tidak ada aksi ban yang dilakukan.*"

    turl = _thumb_url(target)
    if turl:
        embed.set_thumbnail(url=turl)

    # Footer ALWAYS 'external' (to match the screenshot)
    embed.set_footer(text=f"external • {_now_wib_str()}")
    return embed

# Ensure module passes smoke setup import
from discord.ext import commands
class _BanEmbedCog(commands.Cog): ...
async def setup(bot: commands.Bot):
    await bot.add_cog(_BanEmbedCog(bot))