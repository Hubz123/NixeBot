# -*- coding: utf-8 -*-
import os, io, asyncio, logging, re
from typing import Optional
import discord
from discord.ext import commands

try:
    from nixe.helpers.persona_loader import load_persona, pick_line
except Exception:
    load_persona = None
    pick_line = None

try:
    from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes as classify_bytes
except Exception:
    classify_bytes = None

from nixe.helpers.thread_singleton import get_or_create_thread

log = logging.getLogger(__name__)

# Force-replace any old '#ngobrol' / '<#...>' tokens to the correct redirect mention
_CHANNEL_TOKEN = re.compile(r"(?:<#\d+>|#\s*[^\s#]*ngobrol[^\s#]*|\bngobrol\b)", re.IGNORECASE)

def _env_bool_any(*pairs, default=False):
    for k, d in pairs:
        v = os.getenv(k, d)
        if v is None: 
            continue
        if str(v).strip().lower() in ("1","true","yes","on"): 
            return True
        if str(v).strip().lower() in ("0","false","no","off"):
            return False
    return default

def _env_str_any(*keys, default=""):
    for k in keys:
        v = os.getenv(k, None)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

def _env_int_any(*keys, default=0):
    for k in keys:
        v = os.getenv(k, None)
        if v is None: 
            continue
        try:
            return int(str(v).strip())
        except Exception:
            continue
    return default

def _env_float_any(*keys, default=0.0):
    for k in keys:
        v = os.getenv(k, None)
        if v is None: 
            continue
        try:
            return float(str(v).strip())
        except Exception:
            continue
    return default

def _parse_id_list(value: str):
    out = set()
    for tok in (value or "").replace(" ", "").split(","):
        if tok.isdigit(): 
            out.add(int(tok))
    return out

def _provider_threshold(provider: str):
    eps = _env_float_any("LPG_CONF_EPSILON", default=0.0)
    if provider and provider.lower().startswith("gemini"):
        thr = _env_float_any("GEMINI_LUCKY_THRESHOLD", "LPG_GEMINI_THRESHOLD", default=0.80)
        return max(0.0, min(1.0, thr - eps))
    thr = _env_float_any("LPG_GROQ_THRESHOLD", default=0.50)
    return max(0.0, min(1.0, thr - eps))

def _provider_order():
    order = _env_str_any("LPG_PROVIDER_ORDER", "LPG_IMAGE_PROVIDER_ORDER", "LPA_PROVIDER_ORDER", default="gemini,groq")
    return [p.strip().lower() for p in order.split(",") if p.strip()]

def _pick_tone(score: float, tone_env: str) -> str:
    tone_env = (tone_env or "auto").lower()
    if tone_env in ("soft","agro","sharp"): return tone_env
    if score >= 0.95: return "sharp"
    if score >= 0.85: return "agro"
    return "soft"


class LuckyPullGuard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enable = _env_bool_any(("LPG_ENABLE","1"), default=True)

        guards = _env_str_any("LPG_GUARD_CHANNELS", "LUCKYPULL_GUARD_CHANNELS", default="")
        self.guard_channels = _parse_id_list(guards)

        self.redirect_channel_id = _env_int_any("LPG_REDIRECT_CHANNEL_ID", "LUCKYPULL_REDIRECT_CHANNEL_ID", "LPA_REDIRECT_CHANNEL_ID", default=0)

        self.mention = _env_bool_any(("LPG_MENTION","1"), ("LUCKYPULL_MENTION_USER","1"), default=True)
        self.delete_on_guard = _env_bool_any(("LUCKYPULL_DELETE_ON_GUARD","1"), default=True)

        self.provider_order = _provider_order()
        self.timeout_ms = _env_int_any("LUCKYPULL_GEM_TIMEOUT_MS", "LPA_PROVIDER_TIMEOUT_MS", default=20000)

        # Persona config
        self.persona_mode = _env_str_any("LPG_PERSONA_MODE", default="soft")
        self.persona_tone = _env_str_any("LPG_PERSONA_TONE", default="soft")
        self._persona_mode, self._persona_data, self._persona_path = None, {}, None
        try:
            if load_persona:
                m, d, p = load_persona()
                if m and d:
                    self._persona_mode, self._persona_data, self._persona_path = m, d, p
        except Exception:
            pass

        # Singleton whitelist thread settings
        self.whitelist_parent_id = self.redirect_channel_id
        self.whitelist_thread_name = os.getenv("LPG_WHITELIST_THREAD_NAME", "Whitelist LPG (FP)")
        self.whitelist_cache_env = os.getenv("LPG_WHITELIST_THREAD_ENVKEY", "LPG_WHITELIST_THREAD_ID")

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.enable:
            log.warning("[lpg] disabled via LPG_ENABLE=0"); return
        log.warning("[lpg] ready | guards=%s redirect=%s providers=%s timeout=%dms thread_name=%s",
                    list(self.guard_channels), self.redirect_channel_id, self.provider_order, self.timeout_ms,
                    self.whitelist_thread_name)

    def _is_guard_channel(self, channel: discord.abc.GuildChannel) -> bool:
        try:
            return channel and int(channel.id) in self.guard_channels
        except Exception:
            return False

    async def _persona_notify(self, message: discord.Message, score: float):
        tone = _pick_tone(score, self.persona_tone)
        if pick_line and self._persona_data:
            line = pick_line(self._persona_data, self._persona_mode or self.persona_mode, tone)
        else:
            line = "Konten dipindahkan ke channel yang benar."

        redirect_mention = f"<#{self.redirect_channel_id}>" if self.redirect_channel_id else f"#{message.channel.name}"
        user_mention = message.author.mention if self.mention else str(message.author)
        text = f"{user_mention} {line}\n→ silakan post Lucky Pull di {redirect_mention}."
        try:
            await message.channel.send(text, reference=message, mention_author=self.mention)
        except Exception:
            await message.channel.send(text)

    async def _classify(self, img_bytes: bytes):
        """Adapter kompatibel untuk bridge async/sync."""
        if classify_bytes is None:
            return False, 0.0, "none", "bridge_unavailable"
        try:
            res = classify_bytes(img_bytes)
            if hasattr(res, "__await__"):
                res = await res
            if isinstance(res, dict):
                ok = bool(res.get("ok"))
                score = float(res.get("score") or 0.0)
                provider = str(res.get("provider") or "gemini")
                reason = str(res.get("reason") or "")
                return ok, score, provider, reason
            elif isinstance(res, (tuple, list)) and len(res) >= 4:
                ok, score, provider, reason = res[:4]
                return bool(ok), float(score), str(provider), str(reason)
            else:
                return False, 0.0, "none", "invalid_return"
        except Exception as e:
            log.error("[lpg] classify error: %s", e)
            return False, 0.0, "none", f"exception:{e.__class__.__name__}"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.enable or message.author.bot:
            return
        if not self._is_guard_channel(getattr(message, "channel", None)):
            return
        if not message.attachments:
            return

        # First image only
        imgs = [a for a in message.attachments if (a.content_type or "").startswith("image/")]
        if not imgs:
            return
        first = imgs[0]
        try:
            img_bytes = await first.read()
        except Exception:
            return

        ok, score, provider, reason = await self._classify(img_bytes)
        thr = _provider_threshold(provider)
        passed = ok and (score >= thr)
        log.warning("[lpg] chan=%s user=%s score=%.3f thr=%.3f provider=%s pass=%s reason=%s",
                    message.channel.id, message.author, score, thr, provider, passed, reason)
        if not passed:
            return

        # Persona notice first
        await self._persona_notify(message, score)

        # Redirect: just send to redirect channel/thread (no DB)
        if self.redirect_channel_id:
            try:
                to_chan = message.guild.get_channel(self.redirect_channel_id) or await message.guild.fetch_channel(self.redirect_channel_id)
                if to_chan:
                    await to_chan.send(content=f"[redirected] from <#{message.channel.id}> by {message.author.mention}", file=await first.to_file())

            except Exception as e:
                log.error("[lpg] forward failed: %s", e)

        # Delete original if policy says so
        if self.delete_on_guard:
            try:
                await message.delete(delay=0)
            except Exception as e:
                log.error("[lpg] delete failed: %s", e)


# IMPORTANT: use async setup & await add_cog if coroutine (fixes RuntimeWarning)
async def setup(bot: commands.Bot):
    if _env_str_any("LPG_COG_ENABLE", default="1") in ("0","false","no"):
        return
    result = bot.add_cog(LuckyPullGuard(bot))
    if asyncio.iscoroutine(result):
        await result
