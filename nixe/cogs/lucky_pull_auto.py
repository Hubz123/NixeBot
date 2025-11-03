# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import json
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
        ##[LPA:ENV_PRECEDENCE] begin
        def _read_ids(key):
            v = os.getenv(key, "") or ""
            ids = []
            for s in v.split(","):
                s = s.strip()
                if s.isdigit():
                    try: ids.append(int(s))
                    except: pass
            return ids

        # Guards: union of all aliases
        guards = set(_read_ids("LPG_GUARD_CHANNELS")) | set(_read_ids("LPA_GUARD_CHANNELS")) | set(_read_ids("LUCKYPULL_GUARD_CHANNELS"))
        if guards:
            self.guard_channels = tuple(sorted(guards))
        # Optional explicit guard thread ids
        thr_union = set(_read_ids('LPG_GUARD_THREADS')) | set(_read_ids('LPG_GUARD_THREAD_IDS')) | set(_read_ids('LPG_GUARD_THREAD_CHANNELS'))
        self.guard_threads = tuple(sorted(thr_union)) if thr_union else tuple()

        # Redirect channel precedence
        rid = os.getenv("LPA_REDIRECT_CHANNEL_ID") or os.getenv("LPG_REDIRECT_CHANNEL_ID") or os.getenv("LUCKYPULL_REDIRECT_CHANNEL_ID") or ""
        try: self.redirect_channel_id = int(rid) if rid and str(rid).isdigit() else getattr(self, "redirect_channel_id", 0)
        except: pass
        self.redirect_id = getattr(self, "redirect_channel_id", 0)

        # Provider order precedence
        prov = (os.getenv("LPA_PROVIDER_ORDER") or os.getenv("LPG_PROVIDER_ORDER") or os.getenv("LP_PROVIDER_ORDER") or "gemini,groq")
        self.providers = tuple([p.strip() for p in prov.split(",") if p.strip()])

        # Threshold precedence
        def _f(x, d=None):
            try: return float(x)
            except: return d
        thr_del = _f(os.getenv("LPA_THRESHOLD_DELETE"), None)
        if thr_del is None:
            thr_del = _f(os.getenv("GEMINI_LUCKY_THRESHOLD"), None)
        if thr_del is None:
            thr_del = _f(os.getenv("LPG_GEMINI_THRESHOLD"), None)
        self.thr = thr_del if thr_del is not None else getattr(self, "thr", 0.85)

        # Timeout precedence (ms)
        tms = (os.getenv("LPA_PROVIDER_TIMEOUT_MS") or os.getenv("LUCKYPULL_GEM_TIMEOUT_MS") or os.getenv("GEMINI_TIMEOUT_MS") or "20000")
        try: self.timeout_ms = int(tms)
        except: self.timeout_ms = getattr(self, "timeout_ms", 20000)

        # Minimum image bytes (default 8192)
        try: self.min_image_bytes = int(os.getenv("PHISH_MIN_IMAGE_BYTES", "8192"))
        except: self.min_image_bytes = 8192
        
        # Negative keywords for FP guard (from config): LPG_NEGATIVE_TEXT
        try:
            _neg_src = os.getenv("LPG_NEGATIVE_TEXT", "").strip()
            _neg_list = []
            if _neg_src:
                if (_neg_src.startswith("[") and _neg_src.endswith("]")):
                    # JSON array
                    _neg_list = [str(x).strip().lower() for x in json.loads(_neg_src) if str(x).strip()]
                else:
                    # comma/pipe/newline separated
                    tmp = _neg_src.replace("|", ",").replace("\n", ",")
                    _neg_list = [s.strip().lower() for s in tmp.split(",") if s.strip()]
            self.neg_words = tuple(dict.fromkeys(_neg_list))
        except Exception:
            self.neg_words = tuple()
        ##[LPA:ENV_PRECEDENCE] end
        
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
        try:
            # direct channel match
            if int(getattr(ch, 'id', 0)) in self.guard_channels:
                return True
            # thread under a guarded parent channel
            parent = getattr(ch, 'parent', None)
            if parent and int(getattr(parent, 'id', 0)) in self.guard_channels:
                return True
        except Exception:
            pass
        return False

    async def _classify(self, img_bytes: Optional[bytes], text=None) -> Tuple[float,str]:
        try:
            from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes as _bridge  # type: ignore
        except Exception:
            return 0.0, "classifier_unavailable"
        if not img_bytes and not (text or ""):
            return 0.0, "empty"
        try:
            # Call bridge directly; await if coroutine (async-friendly)
            res = _bridge(img_bytes)
            timeout_ms = int(os.getenv("LPA_PROVIDER_TIMEOUT_MS", os.getenv("LUCKYPULL_GEM_TIMEOUT_MS", "45000")))
            if hasattr(res, "__await__"):
                import asyncio
                res = await asyncio.wait_for(res, timeout=timeout_ms/1000.0)

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
        if not m.attachments: return  # image-only: ignore text-only or embed-only
        img_bytes=None
        try:
            if m.attachments:
                img_bytes=await m.attachments[0].read()
                if not img_bytes or len(img_bytes) < self.min_image_bytes:
                    return
        except Exception: img_bytes=None
        async with self._sem:
            prob, via = await self._classify(img_bytes, None)
        fname = ""
        try:
            fname = (m.attachments[0].filename or "").lower()
        except Exception:
            pass
        # Use configured negatives (LPG_NEGATIVE_TEXT) to raise threshold
        _neg = getattr(self, "neg_words", tuple())
        _neg_hit = any((w in fname) for w in _neg) if _neg else False
        eff_thr = self.thr + (0.05 if _neg_hit else 0.0)
        label="lucky" if prob >= eff_thr else "not_lucky"
        _log.info(f"[lpa] classify: result=({label}, {prob:.3f}) thr={eff_thr:.2f} via={via} neg_hit={_neg_hit} neg_words={len(_neg)} file={fname}")
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
        ch = getattr(m, 'channel', None)
        # Accept TextChannel or Thread; use parent of thread for guard check
        try:
            from discord import TextChannel, Thread
            is_text = isinstance(ch, TextChannel)
            is_thread = isinstance(ch, Thread)
        except Exception:
            is_text = hasattr(ch, 'id') and hasattr(ch, 'guild')
            is_thread = hasattr(ch, 'parent') and getattr(ch, 'parent', None) is not None
        if not (is_text or is_thread):
            return
        guard_ch = ch.parent if is_thread else ch
        if not self._is_guard(guard_ch): return
        await self.on_message_inner(m)

    @commands.Cog.listener('on_ready')
    async def _on_ready(self):
        try:
            _log.warning('[lpa] ready guards=%s redirect=%s ttl=%ss',
                          sorted(list(self.guard_channels)), self.redirect_id, self.ttl)
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(LuckyPullAuto(bot))


async def setup(bot):
    from discord.ext import commands as _cmds
    try:
        if bot.get_cog('LuckyPullAuto'):
            return
    except Exception:
        pass
    await bot.add_cog(LuckyPullAuto(bot))
