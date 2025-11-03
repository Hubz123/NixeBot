# -*- coding: utf-8 -*-
from __future__ import annotations
import os, re, time, random, logging, asyncio
from typing import Optional, List, Tuple, Dict
import discord
from discord.ext import commands

_log = logging.getLogger(__name__)

def _env(k: str, default: str = "") -> str:
    v = os.getenv(k)
    return str(v) if v is not None else default

def _csv_ids(v: str):
    out=[]; 
    for tok in (v or "").replace(" ","").split(","):
        if tok.isdigit(): out.append(int(tok))
    return out

def _notice_ttl() -> int:
    v = _env("LPG_PERSONA_NOTICE_TTL") or _env("LPA_PERSONA_NOTICE_TTL") or "10"
    try: return max(3, int(v))
    except: return 10

def _resolve_thr(default: float = 0.85) -> float:
    for k in ("LPA_THRESHOLD_DELETE","GEMINI_LUCKY_THRESHOLD","LPG_LUCKY_THRESHOLD","GROQ_LUCKY_THRESHOLD","LPG_THRESHOLD_DELETE"):
        val = os.getenv(k)
        if val:
            try: return float(val)
            except: pass
    return default

def _redirect_id() -> Optional[int]:
    for k in ("LUCKYPULL_REDIRECT_CHANNEL_ID","LPG_REDIRECT_CHANNEL_ID","LPA_REDIRECT_CHANNEL_ID"):
        v=os.getenv(k)
        if v and v.isdigit(): return int(v)
    return None

async def _redirect_mention(bot: commands.Bot, guild: Optional[discord.Guild]) -> str:
    rid=_redirect_id()
    if not rid: return "#unknown"
    ch=None
    if guild: ch=guild.get_channel(rid)
    if ch is None:
        try: ch=await bot.fetch_channel(rid)
        except: ch=None
    return getattr(ch,"mention",f"<#{rid}>")

_CHANNEL_TOKEN = re.compile(r"(?:<#\d+>|#\s*[^\s#]*ngobrol[^\s#]*|\bngobrol\b)", re.IGNORECASE)

def _expand_vars(text: str, author: discord.Member, redir_channel: Optional[discord.abc.GuildChannel],
                 fallback_channel: discord.abc.GuildChannel, redirect_mention: str) -> str:
    if not text: return text
    out=str(text)
    user_mention=getattr(author,"mention","@user")
    chan = redir_channel or fallback_channel
    chan_m=getattr(chan,"mention","#channel")
    chan_name=getattr(chan,"name","channel")
    parent=getattr(chan,"parent",None)
    parent_m=getattr(parent,"mention",chan_m) if parent else chan_m
    out=re.sub(r"\{\{\s*user\s*\}\}|\{\s*user\s*\}|<\s*user\s*>|\$user|\$USER|\{USER\}", user_mention, out, flags=re.I)
    out=re.sub(r"\{\{\s*channel\s*\}\}|\{\s*channel\s*\}|<\s*channel\s*>|\$channel|\$CHANNEL|\{CHANNEL\}", chan_m, out, flags=re.I)
    out=re.sub(r"\{\{\s*channel_name\s*\}\}|\{\s*channel_name\s*\}", chan_name, out, flags=re.I)
    out=re.sub(r"\{\{\s*parent\s*\}\}|\{\s*parent\s*\}|<\s*parent\s*>|\{PARENT\}", parent_m, out, flags=re.I)
    out=_CHANNEL_TOKEN.sub(redirect_mention, out)
    if user_mention not in out and not re.search(rf"<@!?{getattr(author,'id',0)}>", out):
        out=f"{user_mention} — {out}"
    if redirect_mention not in out:
        if re.search(r"<#\d+>", out): out=re.sub(r"<#\d+>", redirect_mention, out, count=1)
        else:
            sep="" if out.endswith((" ","…",".","!","?",":")) else " "
            out=f"{out}{sep}{redirect_mention}"
    return out

def _normalize_classifier_result(res) -> Tuple[float,str]:
    try:
        if isinstance(res, dict):
            prob=float(res.get("score") or res.get("prob") or res.get("p") or 0.0)
            via=str(res.get("provider") or res.get("via") or "unknown")
            return prob, via
        if isinstance(res,(list,tuple)):
            n=len(res)
            if n>=4: return float(res[1]), str(res[2])
            if n==3: return float(res[0]), str(res[1])
            if n==2: return float(res[0]), str(res[1])
            if n==1: return float(res[0]), "unknown"
        return float(res), "unknown"
    except Exception:
        return 0.0, "normalize_exception"

class LuckyPullAuto(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot=bot
        self.enabled=_env("LPA_ENABLE","1")=="1"
        self._ready_until=time.monotonic()+float(_env("LPA_STARTUP_GATE_SEC","1.5"))
        self.thr=_resolve_thr(0.85)
        self.ttl=_notice_ttl()
        self.timeout_ms=int(_env("LUCKYPULL_GEM_TIMEOUT_MS","20000"))
        self.providers=None
        self._sem=asyncio.Semaphore(int(_env("LPG_CLASSIFY_CONCURRENCY","1") or "1"))
        guards=_env("LUCKYPULL_GUARD_CHANNELS") or _env("LPG_GUARD_CHANNELS")
        self.guard_channels=set(_csv_ids(guards))
        self.redirect_id=_redirect_id()
        self._lines={}
        try:
            from nixe.helpers.persona_loader import load_persona  # type: ignore
            mode,data,path=load_persona()
            payload=data.get(mode) if isinstance(data,dict) and mode in data else data
            if isinstance(payload,dict):
                for k,arr in payload.items():
                    if isinstance(arr,list):
                        self._lines[str(k).lower()]=[str(x) for x in arr if str(x).strip()]
        except Exception: pass
        self._tone_cycle=tuple(self._lines.keys()) or ("soft","agro","sharp")
        self._tone_idx=0

    def _is_guard(self, ch: discord.abc.GuildChannel) -> bool:
        try: return int(ch.id) in self.guard_channels
        except: return False

    async def _classify(self, img_bytes: Optional[bytes], text=None) -> Tuple[float,str]:
        try:
            from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes as _bridge  # type: ignore
        except Exception:
            return 0.0, "classifier_unavailable"
        if not img_bytes and not (text or ""):
            return 0.0, "empty"
        try:
            # Call bridge directly; await if coroutine (async-friendly)
            res = _bridge(img_bytes, text=(text or ""), timeout_ms=self.timeout_ms, providers=self.providers)
            if hasattr(res, "__await__"):
                res = await res
        except asyncio.TimeoutError:
            _log.warning("[lpa] classify timeout after %dms", self.timeout_ms)
            return 0.0, "timeout"
        except Exception as e:
            _log.warning("[lpa] classify exception: %r", e)
            return 0.0, "exception"
        return _normalize_classifier_result(res)

    def _pick_persona_line(self, author, channel, redir_channel, redir_mention) -> str:
        mp=self._lines or {}
        for _ in range(len(self._tone_cycle)):
            tone=self._tone_cycle[self._tone_idx % len(self._tone_cycle)]
            self._tone_idx=(self._tone_idx+1) % max(1,len(self._tone_cycle))
            arr=mp.get(tone) or []
            if arr:
                raw=random.choice(arr)
                line=_expand_vars(raw, author, redir_channel, channel, redir_mention)
                if line: return line
        any_lines=[s for v in mp.values() for s in (v or [])]
        if any_lines:
            raw=random.choice(any_lines)
            return _expand_vars(raw, author, redir_channel, channel, redir_mention)
        return _expand_vars("psst {user}… pindah ke {channel} ya~", author, redir_channel, channel, redir_mention)

    async def on_message_inner(self, m: discord.Message):
        if time.monotonic() < self._ready_until: return
        if not m.attachments and not m.embeds and not (m.content or "").strip(): return
        img_bytes=None
        try:
            if m.attachments:
                img_bytes=await m.attachments[0].read()
        except Exception: img_bytes=None
        async with self._sem:
            prob, via = await self._classify(img_bytes, m.content or "")
        label="lucky" if prob >= self.thr else "not_lucky"
        _log.info(f"[lpa] classify: result=({label}, {prob:.3f}) thr={self.thr:.2f} via={via}")
        if label!="lucky": return
        redir_mention=await _redirect_mention(self.bot, m.guild)
        redir_channel=None
        try:
            if self.redirect_id:
                redir_channel=await self.bot.fetch_channel(self.redirect_id)
        except Exception: redir_channel=None
        line=self._pick_persona_line(m.author, m.channel, redir_channel, redir_mention)
        try: await m.channel.send(line, delete_after=self.ttl)
        except Exception: pass
        if _env("LUCKYPULL_DELETE_ON_GUARD","1")=="1":
            try: await m.delete()
            except Exception: pass

    @commands.Cog.listener()
    async def on_message(self, m: discord.Message):
        if not self.enabled or m.author.bot or not m.guild: return
        if not isinstance(m.channel, discord.TextChannel): return
        if not self._is_guard(m.channel): return
        await self.on_message_inner(m)

async def setup(bot: commands.Bot):
    await bot.add_cog(LuckyPullAuto(bot))
