
import re, json, asyncio, logging, discord, pathlib, os
from discord.ext import commands

_log = logging.getLogger(__name__)

# -------- HYBRID CONFIG (as agreed) --------
_CFG = None
_CFG_MTIME = None
_SECRET_PAT = re.compile(r"(?:^|_)(?:API|TOKEN|KEY|SECRET|PASSWORD|PASS|PRIVATE|CLIENT_SECRET)(?:$|_)", re.I)

def _load_cfg():
    global _CFG, _CFG_MTIME
    cfg_path = pathlib.Path(__file__).resolve().parents[1] / "config" / "runtime_env.json"
    try:
        mt = cfg_path.stat().st_mtime
    except Exception:
        mt = None
    if _CFG is None or mt != _CFG_MTIME:
        try:
            _CFG = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            _CFG = {}
        _CFG_MTIME = mt
    return _CFG

def _env(k: str, d: str|None=None) -> str:
    cfg = _load_cfg()
    allow_json_secrets = (os.getenv("NIXE_ALLOW_JSON_SECRETS","0") == "1"
                          or str(cfg.get("NIXE_ALLOW_JSON_SECRETS","0")) == "1")
    is_secret = bool(_SECRET_PAT.search(k))

    if is_secret and not allow_json_secrets:
        v = os.getenv(k)
        return v if (v is not None and v != "") else (d or "")

    v = cfg.get(k)
    if v is None or v == "":
        v = os.getenv(k)
    return v if (v is not None and v != "") else (d or "")

def _parse_id_list(val: str) -> set[int]:
    ids: set[int] = set()
    for tok in re.split(r"[,\s]+", str(val or "")):
        if not tok: continue
        try: ids.add(int(tok))
        except Exception: pass
    return ids

async def _redirect_mention(bot: discord.Client, guild: discord.Guild) -> str:
    try:
        rid = int(_env("LUCKYPULL_REDIRECT_CHANNEL_ID") or _env("LPG_REDIRECT_CHANNEL_ID") or "0")
        if not rid: return ""
        ch = await bot.fetch_channel(rid)
        if hasattr(ch, "mention"): return ch.mention  # type: ignore[attr-defined]
    except Exception:
        pass
    return ""

class LuckyPullAuto(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = True
        self.thr = float(_env("GEMINI_LUCKY_THRESHOLD", "0.85") or "0.85")
        self.timeout_ms = int(_env("LPG_TIMEOUT_MS", "20000") or "20000")
        self.ttl = int(_env("LPG_TTL", "10") or "10")
        self.redirect_id = int(_env("LUCKYPULL_REDIRECT_CHANNEL_ID") or _env("LPG_REDIRECT_CHANNEL_ID") or "0") or 0
        self.guard_ids: set[int] = _parse_id_list(
            _env("LPG_GUARD_CHANNELS") or _env("LUCKYPULL_GUARD_CHANNELS") or _env("LPA_GUARD_CHANNELS") or _env("GUARD_CHANNELS")
        )
        neg_text = _env("LPG_NEGATIVE_TEXT", "")
        self.neg_words = tuple(w.strip().lower() for w in re.split(r"[\n,]+", str(neg_text)) if w.strip())
        self._sem = asyncio.Semaphore(int(_env("LPG_MAX_CONCURRENCY", "1") or "1"))
        _log.info("[lpa] ready guards=%s redirect=%s ttl=%ss", sorted(self.guard_ids), self.redirect_id, self.ttl)

    # overlay v2 will monkeypatch this
    async def _classify(self, img_bytes: bytes|None, text=None):
        return 0.0, "classifier_unavailable"

    def _is_guard(self, ch) -> bool:
        cid = getattr(ch, "id", None)
        pid = getattr(getattr(ch, "parent", None), "id", None)
        return (cid in self.guard_ids) or (pid in self.guard_ids)

    # === persona line ===
    def _pick_persona_line(self, user: discord.User|discord.Member, ch, redir_channel, redir_mention: str) -> str:
        mention = redir_mention or (redir_channel.mention if redir_channel else "")
        return f"⚠️ Lucky pull tidak boleh di sini. Pindah ke {mention} ya~"

    async def _maybe_await(self, value):
        if asyncio.iscoroutine(value):
            return await value
        return value

    async def on_message_inner(self, m: discord.Message):
        if not m.attachments: return  # image-only
        try:
            buf = await m.attachments[0].read()
        except Exception:
            buf = None
        async with self._sem:
            prob, via = await self._classify(buf, None)
        fname = ""
        try: fname = (m.attachments[0].filename or "").lower()
        except Exception: pass

        _neg = self.neg_words
        _neg_hit = any((w in fname) for w in _neg) if _neg else False
        eff_thr = self.thr + (0.05 if _neg_hit else 0.0)

        label = "lucky" if prob >= eff_thr else "not_lucky"
        _log.info(f"[lpa] classify: result=({label}, {prob:.3f}) thr={eff_thr:.2f} via={via} neg_hit={_neg_hit} neg_words={len(_neg)} file={fname}")
        if label != "lucky": return

        redir_mention = await _redirect_mention(self.bot, m.guild)
        redir_channel = None
        try:
            if self.redirect_id:
                redir_channel = await self.bot.fetch_channel(self.redirect_id)
        except Exception:
            redir_channel = None

        # Persona overlay may patch this as async -> await if needed
        line = self._pick_persona_line(m.author, m.channel, redir_channel, redir_mention)
        line = await self._maybe_await(line)
        if not isinstance(line, str):
            line = str(line)

        try:
            await m.channel.send(line, delete_after=self.ttl)
        except Exception:
            pass
        if _env("LUCKYPULL_DELETE_ON_GUARD", "1") == "1":
            try: await m.delete()
            except Exception: pass

    @commands.Cog.listener()
    async def on_message(self, m: discord.Message):
        if not self.enabled or m.author.bot or not m.guild: return
        ch = getattr(m, "channel", None)
        try:
            from discord import TextChannel, Thread
            is_text = isinstance(ch, TextChannel)
            is_thread = isinstance(ch, Thread)
        except Exception:
            is_text = hasattr(ch, "id") and hasattr(ch, "guild")
            is_thread = hasattr(ch, "parent") and getattr(ch, "parent", None) is not None
        if not (is_text or is_thread):
            return
        if not self._is_guard(ch):
            _log.debug("[lpa] skip: ch_id=%s parent_id=%s", getattr(ch,"id",None), getattr(getattr(ch,"parent",None),"id",None))
            return
        await self.on_message_inner(m)

    @commands.Cog.listener('on_ready')
    async def _on_ready(self):
        _log.info("[lpa] re-assert INFO after ready")

async def setup(bot: commands.Bot):
    await bot.add_cog(LuckyPullAuto(bot))
