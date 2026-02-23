
from __future__ import annotations
import logging
from typing import Optional, Union
import discord
from datetime import datetime, timedelta


import io, json
from typing import Any, Dict

def build_ban_evidence_payload(
    *,
    guild: Optional[discord.Guild],
    target: Union[discord.User, discord.Member, None],
    moderator: Union[discord.User, discord.Member, None],
    reason: Optional[str],
    evidence: Optional[dict],
) -> Dict[str, Any]:
    """Create a JSON-serializable evidence payload for ban logs.
    Always returns a dict (may include empty evidence)."""
    ev = dict(evidence or {})
    # Resolve channel name for readability (best-effort).
    try:
        cid = int(ev.get('channel_id') or 0)
    except Exception:
        cid = 0
    ch_name = ''
    if cid and guild:
        ch_obj = None
        try:
            get_cot = getattr(guild, 'get_channel_or_thread', None)
            if callable(get_cot):
                ch_obj = get_cot(cid)
            if not ch_obj:
                ch_obj = guild.get_channel(cid)
        except Exception:
            ch_obj = None
        ch_name = str(getattr(ch_obj, 'name', '') or '').strip() if ch_obj else ''
    if cid and ch_name:
        ev['channel_name'] = ch_name
    elif cid and 'channel_name' not in ev:
        ev['channel_name'] = ''
    return {
        "ts_wib": _now_wib_str(),
        "guild_id": int(getattr(guild, "id", 0) or 0) if guild else 0,
        "channel_id": int(ev.get("channel_id") or 0) if isinstance(ev, dict) else 0,
        "channel_name": str(ev.get("channel_name") or "") if isinstance(ev, dict) else "",
        "target_id": int(getattr(target, "id", 0) or 0) if target else 0,
        "target_name": _safe_name(target),
        "moderator_id": int(getattr(moderator, "id", 0) or 0) if moderator else 0,
        "moderator_name": _safe_name(moderator),
        "reason": (reason or "").strip(),
        "evidence": ev,
    }

def build_ban_evidence_file(payload: Dict[str, Any], *, filename: str) -> discord.File:
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return discord.File(fp=io.BytesIO(data), filename=filename)
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
            # Prefer evidence passed from caller to avoid double-pop.
            ev = None
            try:
                ev = kwargs.get("evidence", None)
            except Exception:
                ev = None
            if not ev:
                from nixe.helpers import phish_evidence_cache as _pec
                ev = _pec.pop(int(getattr(guild, "id", 0) or 0), int(getattr(target, "id", 0) or 0))
            if ev:
                lines_ev = []
                # Include source channel (name, not raw ID) to make audits easier.
                try:
                    cid = int(ev.get("channel_id") or 0)
                except Exception:
                    cid = 0
                if cid and guild:
                    ch_obj = None
                    try:
                        # discord.py 2.x provides get_channel_or_thread
                        get_cot = getattr(guild, "get_channel_or_thread", None)
                        if callable(get_cot):
                            ch_obj = get_cot(cid)
                        if not ch_obj:
                            ch_obj = guild.get_channel(cid)
                    except Exception:
                        ch_obj = None
                    ch_name = str(getattr(ch_obj, "name", "") or "").strip() if ch_obj else ""
                    prov = str(ev.get("provider") or "").strip().lower()
                    label = "Touchdown" if "touchdown" in prov or "first-touchdown" in prov else "Channel"
                    if ch_name:
                        lines_ev.append(f"{label}: #{ch_name}")
                    else:
                        # Fallback: still show mention if we cannot resolve name
                        lines_ev.append(f"{label}: <#{cid}>")

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

def build_classification_embed(
    *,
    title: str,
    result: str,
    score: Optional[float],
    provider: str,
    reason: str,
    message_id: int = 0,
    channel_display: str = "",
    jump_url: str = "",
    image_attachment_name: Optional[str] = None,
    extra_lines: Optional[list[str]] = None,
) -> discord.Embed:
    """Build a classification-style embed (similar layout to LPG classification cards)."""
    embed = discord.Embed(title=title, color=discord.Color.red())
    # Top row-like fields
    embed.add_field(name="Result", value=str(result), inline=True)
    if score is None:
        embed.add_field(name="Score", value="—", inline=True)
    else:
        try:
            embed.add_field(name="Score", value=f"{float(score):.3f}", inline=True)
        except Exception:
            embed.add_field(name="Score", value=str(score), inline=True)
    embed.add_field(name="Provider", value=(provider or "—"), inline=True)

    embed.add_field(name="Reason", value=(reason or "—"), inline=False)

    if message_id:
        embed.add_field(name="Message ID", value=str(int(message_id)), inline=True)
    if channel_display:
        embed.add_field(name="Channel", value=channel_display, inline=True)

    if jump_url:
        embed.add_field(name="Evidence", value=jump_url, inline=False)

    if extra_lines:
        txt = "\n".join([str(x) for x in extra_lines if str(x).strip()])
        if txt:
            if len(txt) > 900:
                txt = txt[:897] + "…"
            embed.add_field(name="Indicators", value=txt, inline=False)

    if image_attachment_name:
        try:
            embed.set_image(url=f"attachment://{image_attachment_name}")
        except Exception:
            pass

    embed.set_footer(text=f"external • {_now_wib_str()}")
    return embed

