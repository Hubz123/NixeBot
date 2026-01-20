# -*- coding: utf-8 -*-
from __future__ import annotations

import os, logging, json, re, asyncio
import discord
import aiohttp
from discord.ext import commands

from nixe.helpers import banlog

log = logging.getLogger("nixe.cogs.phish_ban_embed")

EMBED_COLOR = int(os.getenv("PHISH_EMBED_COLOR", "16007990"))  # default orange 0xF4511E
DELETE_AFTER_SECONDS = int(os.getenv("PHISH_EMBED_TTL", os.getenv("BAN_EMBED_TTL_SEC", "3600")))
AUTO_BAN = (os.getenv("PHISH_AUTO_BAN", "0") == "1" or os.getenv("PHISH_AUTOBAN", "0") == "1")
PHISH_AUTOBAN_ON_PHASH = (os.getenv("PHISH_AUTOBAN_ON_PHASH", "0").strip().lower() in ("1","true","yes","on"))
PHISH_REQUIRE_OCR_FOR_BAN = (os.getenv("PHISH_REQUIRE_OCR_FOR_BAN", "1").strip().lower() in ("1","true","yes","on"))
PHISH_GROQ_CONFIRM_MIN_CONF = float(os.getenv("PHISH_GROQ_CONFIRM_MIN_CONF", "0.85") or "0.85")
PHISH_GROQ_CONFIRM_MAX_IMAGES = int(os.getenv("PHISH_GROQ_CONFIRM_MAX_IMAGES", "2") or "2")
GROQ_API_KEY = os.getenv("GROQ_API_KEY","")
GROQ_MODEL_VISION = os.getenv("GROQ_MODEL_VISION") or "meta-llama/llama-4-scout-17b-16e-instruct"

DELETE_MESSAGE = (os.getenv("PHISH_DELETE_MESSAGE", "1") == "1")

# When BAN_UNIFIER_ENABLE=1 we normally let BanTemplateUnifier handle the
# pretty external-style embed and suppress this technical embed to avoid
# duplicate messages. PHISH_EMBED_FORCE=1 can override this behaviour.
DISABLE_SELF_EMBED = False



class PhishBanEmbed(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        log.info(
            "[phish-ban-embed] ready auto_ban=%s delete_message=%s disable_self_embed=%s",
            AUTO_BAN,
            DELETE_MESSAGE,
            DISABLE_SELF_EMBED,
        )


    async def _groq_confirm_phish_urls(self, urls: list[str]) -> dict:
        """Groq Vision confirmation used to gate bans (anti-false-positive).
        Returns dict: {phish: bool, confidence: float, reason: str, signals: list[str]}.
        """
        urls = [u for u in (urls or []) if isinstance(u, str) and u.strip()]
        if not urls or not GROQ_API_KEY:
            return {"phish": False, "confidence": 0.0, "reason": "groq-not-configured", "signals": []}

        urls = urls[: max(1, min(PHISH_GROQ_CONFIRM_MAX_IMAGES, 4))]

        prompt = (
            "You are a high-precision phishing/scam detector for Discord images.\n"
            "These scams often mimic 'withdraw success', 'bonus', 'claim', 'redeem', 'promo code', "
            "crypto giveaway (e.g., USDT), casino/betting offers, or impersonate brands/admins.\n"
            "Classify phish=true ONLY if there are clear indicators of a scam or deceptive intent.\n"
            "If it is a normal photo, artwork, game screenshot, harmless promo, or meme, return phish=false.\n"
            "Return STRICT JSON only: "
            '{"phish":true/false, "confidence":0.0-1.0, "reason":"short", "signals":["..."]}.'
        )

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}

        best = {"phish": False, "confidence": 0.0, "reason": "no-match", "signals": []}

        timeout = aiohttp.ClientTimeout(total=float(os.getenv("PHISH_GROQ_CONFIRM_TIMEOUT_S", "6") or "6"))
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            for u in urls:
                payload = {
                    "model": GROQ_MODEL_VISION,
                    "temperature": 0.0,
                    "max_tokens": 200,
                    "messages": [
                        {"role": "system", "content": "You are a strict, high-precision phishing detector."},
                        {"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": u}},
                        ]},
                    ],
                }
                try:
                    async with sess.post(url, headers=headers, json=payload) as resp:
                        data = await resp.json(content_type=None)
                except Exception:
                    continue

                txt = ""
                try:
                    txt = (data["choices"][0]["message"]["content"] or "").strip()
                except Exception:
                    txt = ""

                phish = False
                conf = 0.0
                reason = (txt or "")[:200]
                signals: list[str] = []

                try:
                    m = re.search(r"\{.*\}", txt, flags=re.S)
                    obj = json.loads(m.group(0) if m else txt)
                    if isinstance(obj, dict):
                        phish = bool(obj.get("phish", False))
                        try:
                            conf = float(obj.get("confidence", 0.0) or 0.0)
                        except Exception:
                            conf = 0.0
                        if "reason" in obj:
                            reason = str(obj.get("reason") or "")[:200]
                        sig = obj.get("signals", [])
                        if isinstance(sig, list):
                            signals = [str(x)[:64] for x in sig][:8]
                except Exception:
                    lower = (txt or "").lower()
                    if re.search(r'\"phish\"\s*:\s*true', lower):
                        phish = True
                        conf = max(conf, 0.7)

                if phish and conf > float(best["confidence"]):
                    best = {"phish": True, "confidence": conf, "reason": reason, "signals": signals}

                if phish and conf >= PHISH_GROQ_CONFIRM_MIN_CONF:
                    return {"phish": True, "confidence": conf, "reason": reason, "signals": signals}

        return best

    @commands.Cog.listener("on_nixe_phish_detected")
    async def on_nixe_phish_detected(self, payload: dict) -> None:
        """Handle internal phishing detection events.

        Payload keys (best-effort, all optional):
        - guild_id, channel_id, message_id, user_id
        - provider, score, reason
        - evidence: list[str] of attachment names / URLs
        """
        try:
            gid = payload.get("guild_id")
            mid = payload.get("message_id")
            cid = payload.get("channel_id")
            uid = payload.get("user_id")
            provider = payload.get("provider") or "phash"
            try:
                score = float(payload.get("score") or 0.0)
            except Exception:
                score = 0.0
            reason = str(payload.get("reason") or "")
            evidence = payload.get("evidence") or []


            # Best-effort: store human-readable evidence for BanTemplateUnifier embeds.
            try:
                from nixe.helpers import phish_evidence_cache as _pec
                _pec.record_from_payload(payload, provider=str(provider or ""), reason=str(reason or "")[:180])
            except Exception:
                pass

            kind = str(payload.get("kind") or "").strip().lower()
            # IMPORTANT POLICY:
            # - Groq vision results are log-only (to avoid false positives).
            # - Delete/Ban actions are allowed ONLY for pHash matches.
            is_phash_confirmed = (provider.lower() == "phash") or kind.startswith("phash")
            is_phash_autolearn = (provider.lower() == "phash-autolearn")
            allow_log = bool(is_phash_confirmed or is_phash_autolearn)
            allow_delete = bool(is_phash_confirmed)



            guild = self.bot.get_guild(int(gid)) if gid else None
            channel = self.bot.get_channel(int(cid)) if cid else None

            user = None
            if guild and uid:
                try:
                    user = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                except Exception:
                    user = None
            # Ban gating: require pHash confirmed AND (optionally) Groq OCR/Vision confirmation
            ocr_gate = None
            ban_allowed = bool(allow_delete and AUTO_BAN and guild and user and PHISH_AUTOBAN_ON_PHASH)
            if ban_allowed and is_phash_confirmed and PHISH_REQUIRE_OCR_FOR_BAN:
                try:
                    ocr_gate = await self._groq_confirm_phish_urls([str(x) for x in (evidence or []) if isinstance(x, str)])
                    ok = bool(ocr_gate.get("phish")) and float(ocr_gate.get("confidence") or 0.0) >= PHISH_GROQ_CONFIRM_MIN_CONF
                    if not ok:
                        ban_allowed = False
                except Exception:
                    # On any OCR error, do NOT ban (fail-closed for safety / false-positive avoidance).
                    ban_allowed = False


            # Optional technical embed just for phishing log channel
            if not DISABLE_SELF_EMBED:
                title = "💀 Phishing Detected"
                em = discord.Embed(
                    title=title,
                    color=EMBED_COLOR,
                    timestamp=discord.utils.utcnow(),
                )
                em.add_field(
                    name="User",
                    value=f"<@{uid}>" if uid else "-",
                    inline=True,
                )
                em.add_field(
                    name="Provider",
                    value=str(provider),
                    inline=True,
                )
                em.add_field(
                    name="Score",
                    value=f"{score:.2f}",
                    inline=True,
                )

                if ocr_gate is not None:
                    try:
                        conf = float(ocr_gate.get("confidence") or 0.0)
                    except Exception:
                        conf = 0.0
                    ph = bool(ocr_gate.get("phish"))
                    rsn = str(ocr_gate.get("reason") or "")[:200]
                    em.add_field(
                        name="Groq OCR Confirm",
                        value=f"{'PHISH' if ph else 'NO'} | conf={conf:.2f}\n{rsn}",
                        inline=False,
                    )
                if reason:
                    em.add_field(
                        name="Reason",
                        value=reason[:512],
                        inline=False,
                    )
                if evidence:
                    ev_lines = [str(x) for x in evidence[:5]]
                    em.add_field(
                        name="Evidence",
                        value="\n".join(ev_lines),
                        inline=False,
                    )
                if gid and cid and mid:
                    em.add_field(
                        name="Message",
                        value=f"https://discord.com/channels/{gid}/{cid}/{mid}",
                        inline=False,
                    )

                # Send embed to ban-log channel by default (avoid spam in user channels).
                send_to_origin = (os.getenv("PHISH_EMBED_SEND_TO_ORIGIN", "0") == "1")
                target = None
                if guild:
                    try:
                        target = await banlog.get_ban_log_channel(guild)
                    except Exception:
                        target = None
                if send_to_origin and channel:
                    target = channel

                if target:
                    try:
                        await target.send(embed=em, delete_after=DELETE_AFTER_SECONDS)
                    except Exception:
                        # Logging not critical – continue with delete/ban path
                        pass

            # Auto delete offending message (pHash-only; best-effort, optional)
            # Resolve safe data thread (never delete the mirror/data thread)
            safe_data_thread = 0
            try:
                safe_data_thread = int(
                    os.getenv("PHISH_DATA_THREAD_ID")
                    or os.getenv("NIXE_PHISH_DATA_THREAD_ID")
                    or os.getenv("PHASH_IMAGEPHISH_THREAD_ID")
                    or "0"
                )
            except Exception:
                safe_data_thread = 0

            if allow_delete and DELETE_MESSAGE and channel and mid:
                try:
                    if not safe_data_thread or int(channel.id) != safe_data_thread:
                        msg = await channel.fetch_message(int(mid))
                        await msg.delete()
                except Exception:
                    pass


            # Auto-ban (pHash confirmed; optionally requires Groq OCR/Vision confirmation).
            # NOTE: ban_allowed already applies PHISH_AUTOBAN_ON_PHASH and the OCR gate when enabled.
            if ban_allowed:
                try:
                    ocr_note = ""
                    if ocr_gate is not None:
                        ocr_note = f" | groq_conf={float(ocr_gate.get('confidence') or 0.0):.2f}"
                    # Purge recent history (Discord supports up to 7 days)
                    try:
                        days = int(os.getenv("PHISH_DELETE_MESSAGE_DAYS", "0") or "0")
                    except Exception:
                        days = 0
                    days = max(0, min(days, 7))
                    seconds = int(days * 86400)
            
                    try:
                        await guild.ban(
                            user,
                            reason=f"Phishing confirmed (pHash+OCR): {reason[:120]}{ocr_note}",
                            delete_message_seconds=seconds,
                        )
                    except TypeError:
                        await guild.ban(
                            user,
                            reason=f"Phishing confirmed (pHash+OCR): {reason[:120]}{ocr_note}",
                            delete_message_days=days,
                        )
                except Exception:
                    pass
        except Exception as e:
            log.debug("[phish-ban-embed] err: %r", e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PhishBanEmbed(bot))
