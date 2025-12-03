# nixe/cogs/a00_phish_first_touchdown_autoban.py
# First-touchdown phishing autoban (silent). Handles:
#  - postimg.cc links
#  - identical attachment packs (>=3, same sanitized filename, all images, similar sizes, no meaningful text)
#  - disguised WEBP files repeatedly named 'image.png' (>=3) by magic bytes sniff
import os, re, io, discord
from discord.ext import commands
from collections import Counter

def _getenv(k,d=""): return os.getenv(k,d)
def _csv(v): return [x.strip() for x in (v or "").split(",") if x.strip()]

POSTIMG = re.compile(r"(?i)\bhttps?://(?:i\.)?postimg\.cc/[^\s>]+")

def _strip_urls(text: str) -> str:
    if not text: return ""
    return re.sub(r"https?://\S+", "", text)

def _meaningful_text_len(text: str) -> int:
    t = _strip_urls(text)
    t = re.sub(r"[^\w\s]+", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return len(t)

def _sanitize_name(name: str) -> str:
    n = name.lower()
    n = re.sub(r"\s*\(\d+\)$", "", n)        # "image (1).png" -> "image.png"
    n = re.sub(r"\s*-\s*copy$", "", n)       # "image - copy.png" -> "image.png"
    return n

def _sizes_within_pct(sizes, pct=5.0) -> bool:
    sizes = [s for s in sizes if isinstance(s, int) and s > 0]
    if len(sizes) < 2: return False
    mn, mx = min(sizes), max(sizes)
    if mn == 0: return False
    return (mx - mn) / mn <= (pct / 100.0)

def _all_images(attachments) -> bool:
    for a in attachments:
        ct = (getattr(a, "content_type", None) or "").lower()
        if not ct.startswith("image/"):
            return False
    return True

def _is_webp_magic(b: bytes) -> bool:
    # 'RIFF' + 4 bytes + 'WEBP'
    return len(b) >= 12 and b[:4] == b'RIFF' and b[8:12] == b'WEBP'

class PhishFirstTouchdownAutoban(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        default_scope = "886534544688308265,1293212357362716733"
        self.scope = set(_csv(_getenv("FIRST_TOUCHDOWN_CHANNELS", default_scope)))

    def _in_scope(self, ch):
        if not ch:
            return False
        try:
            cid = int(getattr(ch, "id", 0) or 0)
        except Exception:
            return False
        try:
            pid = int(getattr(ch, "parent_id", 0) or 0)
        except Exception:
            pid = 0
        # Never apply first-touchdown autoban inside threads.
        if pid:
            return False
        if not self.scope:
            return True
        return str(cid) in self.scope

    def _has_postimg_link(self, m: discord.Message) -> bool:
        text = m.content or ""
        for e in m.embeds:
            if e.url: text += "\n" + (e.url or "")
            if e.title: text += "\n" + (e.title or "")
            if e.description: text += "\n" + (e.description or "")
        return bool(POSTIMG.search(text))

    def _attachments_identical_pack(self, m: discord.Message) -> bool:
        atts = [a for a in m.attachments if a.filename]
        if len(atts) < 3: return False
        names = [_sanitize_name(a.filename) for a in atts]
        # all filenames identical (after sanitization)
        if len(set(names)) != 1: return False
        # all images
        if not _all_images(atts): return False
        # sizes within 5%
        sizes = [getattr(a, "size", 0) for a in atts]
        if not _sizes_within_pct(sizes, pct=5.0): return False
        # message has no meaningful text (URLs don't count)
        if _meaningful_text_len(m.content or "") > 8: return False
        return True

    async def _attachments_disguised_webp_pack(self, m: discord.Message) -> bool:
        atts = [a for a in m.attachments if a.filename]
        if len(atts) < 3: return False
        # focus on 'image.png' spam but accept minor variants like 'image (1).png'
        names = [_sanitize_name(a.filename) for a in atts]
        if any(not n.endswith(".png") for n in names): 
            return False
        # sniff first bytes for WEBP magic
        webp_count = 0
        for a in atts:
            try:
                data = await a.read()
                if _is_webp_magic(data[:16]):
                    webp_count += 1
            except Exception:
                pass
        if webp_count >= 3:
            # optional: sizes close too => stronger signal
            sizes = [getattr(a, "size", 0) for a in atts]
            if _sizes_within_pct(sizes, pct=10.0) or _meaningful_text_len(m.content or "") <= 8:
                return True
        return False

    def _is_phish_sync(self, m: discord.Message) -> bool:
        if self._has_postimg_link(m): return True
        if self._attachments_identical_pack(m): return True
        return False

    async def _is_phish(self, m: discord.Message) -> bool:
        # quick sync checks first
        if self._is_phish_sync(m):
            return True
        # heavier check: disguised WEBP pack
        if await self._attachments_disguised_webp_pack(m):
            return True
        return False

    async def _ban_and_embed(self, m: discord.Message):
        try:
            await m.guild.ban(m.author, reason="Auto-ban phishing (first touchdown)", delete_message_days=1)
        except Exception:
            pass
        try:
            em = discord.Embed(
                title="🔨 Auto-ban: Phishing Detected",
                description="User dibanned otomatis (first touchdown).",
                color=0xD72638
            )
            em.add_field(name="User", value=f"{m.author.mention} (`{m.author.id}`)", inline=False)
            links = [u for u in (m.content or '').split() if u.startswith('http')]
            if links: em.add_field(name="Links", value="\n".join(links[:6]), inline=False)
            if m.attachments:
                names = ", ".join(a.filename for a in m.attachments[:6])
                em.add_field(name="Attachments", value=names, inline=False)
            await m.channel.send(embed=em)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_message(self, m: discord.Message):
        if not m.guild or m.author.bot: return
        if not self._in_scope(m.channel): return
        if not (m.content or m.attachments or m.embeds): return
        if await self._is_phish(m):
            await self._ban_and_embed(m)

async def setup(bot: commands.Bot):
    await bot.add_cog(PhishFirstTouchdownAutoban(bot))
