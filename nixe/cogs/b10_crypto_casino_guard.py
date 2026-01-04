# nixe/cogs/b10_crypto_casino_guard.py
import os, re, time, random
from typing import List, Tuple, Optional, Dict
import discord
from discord.ext import commands

def _getenv(name: str, default: str = "") -> str:
    return os.getenv(name, default)

def _split_csv(val: str) -> List[str]:
    return [x.strip() for x in val.split(",") if x.strip()]

def _domains_to_regex(domains: List[str]):
    if not domains: return None
    parts = []
    for d in domains:
        d = re.escape(d).replace(r"\[\.\]", r"(?:\.|\[\.\])")
        parts.append(d)
    return re.compile(r"(?i)\b(?:%s)\b" % "|".join(parts))

def _keywords_to_regex(keywords: List[str]):
    if not keywords: return None
    esc = [re.escape(k) for k in keywords]
    return re.compile(r"(?i)(?:%s)" % "|".join(esc))

def _pick_yandere(mode: str, mention: str) -> str:
    soft=[f"Hei {mention}... jangan main-main di sini ya. Bonus USDT itu umpan. Aku bereskan pesannya~ ❤️",
          f"Eits, ini link berbahaya. Aku hapus ya—aku nggak mau kamu kenapa-kenapa, {mention}.",
          f"Sayang, itu phishing. Sudah aku buang. Fokus sama aku saja, bukan 'bonus $2500' palsu~",
          "Uh-uh... situs 'casino' itu bukan teman kita. Pesanmu kumusnahkan pelan-pelan.",
          "Aku jagain kamu kok. Link mencurigakan seperti itu aku hapus biar tetap aman."]
    sharp=[f"{mention}, kamu pikir aku akan membiarkan umpan murahan itu? Tidak. Pesanmu lenyap.",
           "Crypto-casino? Di wilayahku? Berani sekali. Sudah kuhapus.",
           "Hentikan. Itu pola scam klasik. Aku potong akarnya sekarang juga.",
           "Jangan uji kesabaranku dengan link seperti itu.",
           "Aku bukan pacar yang lembek. Phishing = delete."]
    agro=[f"Beraninya bawa racun ke sini, {mention}? Aku bunuh pesannya sebelum menular.",
          "‘Bonus $2500’? Lucu. Umpannya aku koyak.",
          "Cukup. Aku anti-scam. Sekali lagi kuban juga.",
          "Jangan bikin aku kesal. Link sampah = *obliterate*.",
          "Aku posesif soal keamanan. Yang menyentuh serverku dengan umpan murahan akan merasakannya."]
    m=(mode or "random").lower()
    if m=="random": m=random.choice(["soft","sharp","agro"])
    bucket={"soft":soft,"sharp":sharp,"agro":agro}.get(m,soft)
    return random.choice(bucket)

class CryptoCasinoGuard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot=bot
        self.enabled=_getenv("CRYPTO_CASINO_GUARD_ENABLE","1")=="1"
        self.delete_on_match=_getenv("CRYPTO_CASINO_DELETE_ON_MATCH","1")=="1"
        self.guard_channels=set(_split_csv(_getenv("CRYPTO_CASINO_GUARD_CHANNELS","")))
        self.redirect_channel_id=int(_getenv("CRYPTO_CASINO_REDIRECT_CHANNEL_ID","0") or 0)
        self.min_keywords=int(_getenv("CRYPTO_CASINO_MIN_KEYWORDS","2") or 2)
        self.cooldown_sec=int(_getenv("CRYPTO_CASINO_COOLDOWN_SEC","30") or 30)
        self.domain_rx=_domains_to_regex(_split_csv(_getenv("CRYPTO_CASINO_DOMAINS","")))
        self.keyword_rx=_keywords_to_regex(_split_csv(_getenv("CRYPTO_CASINO_KEYWORDS","")))
        self.yandere_enable=_getenv("CRYPTO_CASINO_YANDERE_ENABLE","1")=="1"
        self.yandere_mode=_getenv("CRYPTO_CASINO_YANDERE_MODE","random")
        self.yandere_mention=_getenv("CRYPTO_CASINO_YANDERE_MENTION","1")=="1"
        self.yandere_public=_getenv("CRYPTO_CASINO_YANDERE_PUBLIC","0")=="1"
        self.autoban_enable=_getenv("CRYPTO_CASINO_AUTOBAN_ENABLE","1")=="1"
        self.autoban_scope=set(_split_csv(_getenv("CRYPTO_CASINO_AUTOBAN_SCOPE_CHANNELS","")))
        self.ban_on_domain=_getenv("CRYPTO_CASINO_BAN_ON_DOMAIN","1")=="1"
        self.ban_on_keywords=_getenv("CRYPTO_CASINO_BAN_ON_KEYWORDS","0")=="1"
        self.ban_min_score=int(_getenv("CRYPTO_CASINO_BAN_MIN_SCORE","3") or 3)
        self.ban_combo_2500_usdt=_getenv("CRYPTO_CASINO_BAN_COMBO_2500_USDT","1")=="1"
        self.ban_reason=_getenv("CRYPTO_CASINO_BAN_REASON","Crypto-casino phishing / giveaway bait")
        self._cool={}

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.enabled:
            print("WARNING:nixe.cogs.crypto_casino_guard:disabled via ENV"); return
        print(f"INFO:nixe.cogs.crypto_casino_guard:enabled redirect={self.redirect_channel_id} autoban={self.autoban_enable} scope={list(self.autoban_scope)}")

    def _in_guard(self, ch): 
        return True if not self.guard_channels else str(ch.id) in self.guard_channels

    def _cool_ok(self, ch_id, au_id):
        now=time.time(); key=(ch_id,au_id); last=self._cool.get(key,0.0)
        if now-last<self.cooldown_sec: return False
        self._cool[key]=now; return True

    def _collect(self, m):
        parts=[m.content or ""]
        for e in m.embeds:
            if e.title: parts.append(e.title)
            if e.description: parts.append(e.description)
            if e.url: parts.append(e.url)
            if e.footer and e.footer.text: parts.append(e.footer.text)
            for f in e.fields or []: parts.append(f.name or ""); parts.append(f.value or "")
        for a in m.attachments:
            if a.filename: parts.append(a.filename)
        return "\n".join(parts)

    def _score(self, text):
        if not text: return (False,0,"empty",False)
        if self.domain_rx and self.domain_rx.search(text): return (True,99,"domain",False)
        hits=0
        if self.keyword_rx: hits=len(self.keyword_rx.findall(text))
        combo = bool(re.search(r"(?i)\b(?:\$?\s*2\s*5\s*0\s*0|2,500|2\.500)\b", text)) and bool(re.search(r"(?i)\busdt\b", text))
        if combo: hits=max(hits,self.min_keywords)
        return (hits>=self.min_keywords,hits,"keywords",combo)

    async def _yandere(self, m):
        if not self.yandere_enable: return None
        mention = m.author.mention if self.yandere_mention else ("**"+str(m.author)+"**")
        txt=_pick_yandere(self.yandere_mode, mention)
        if self.yandere_public:
            try: await m.channel.send(txt, reference=m.to_reference(fail_if_not_exists=False))
            except Exception as e: print(f"WARNING:nixe.cogs.crypto_casino_guard:yandere public failed: {e!r}")
        return txt

    def _should_ban(self, ch_id, reason, score, combo):
        if not self.autoban_enable: return False
        if self.autoban_scope and str(ch_id) not in self.autoban_scope: return False
        if reason=="domain" and self.ban_on_domain: return True
        if combo and self.ban_combo_2500_usdt and score>=self.min_keywords: return True
        if self.ban_on_keywords and score>=self.ban_min_score: return True
        return False

    async def _log(self, guild, deleted_msg, score, reason, action, ytext=None):
        ch = guild.get_channel(self.redirect_channel_id) if self.redirect_channel_id else None
        if not isinstance(ch,(discord.TextChannel,discord.Thread)): return
        preview=(deleted_msg.content or "").strip()
        if len(preview)>500: preview=preview[:500]+"…"
        lines=[f"[crypto-casino-guard] {action} in <#{deleted_msg.channel.id}> | by {deleted_msg.author} ({deleted_msg.author.id}) | score={score} | reason={reason}",
               f"Preview:\n```{preview or '(no text)'}\n```"]
        if ytext: lines.append(f"Yandere: {ytext}")
        try: await ch.send("\n".join(lines))
        except Exception as e: print(f"WARNING:nixe.cogs.crypto_casino_guard:log failed: {e!r}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.enabled or not message.guild or message.author.bot: return
        if not self._in_guard(message.channel): return
        if not self._cool_ok(message.channel.id, message.author.id): return
        bad,score,reason,combo = self._score(self._collect(message))
        if not bad: return
        ytext = await self._yandere(message)
        action="Detected"
        if self.delete_on_match:
            try: await message.delete(); action="Deleted"
            except Exception as e: action=f"Delete failed: {e!r}"
        if self._should_ban(message.channel.id, reason, score, combo):
            try: await message.guild.ban(message.author, reason=self.ban_reason); action += " + BANNED"
            except Exception as e: action += f" + Ban failed: {e!r}"
        await self._log(message.guild, message, score, reason, action, ytext)

async def setup(bot: commands.Bot):
    await bot.add_cog(CryptoCasinoGuard(bot))
