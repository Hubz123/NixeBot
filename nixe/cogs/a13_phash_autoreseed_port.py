
import os, json, asyncio, discord
from discord.ext import commands, tasks
from discord import AllowedMentions
from nixe.helpers import img_hashing
from nixe.helpers.safe_delete import safe_delete

PHASH_DB_MARKER = os.getenv("PHASH_DB_MARKER", "NIXE_PHASH_DB_V1").strip()
DB_MSG_ID = int(os.getenv("PHASH_DB_MESSAGE_ID", "0") or "0")
TARGET_THREAD_ID = int(
    os.getenv("PHASH_IMAGEPHISH_THREAD_ID")
    or os.getenv("NIXE_PHASH_SOURCE_THREAD_ID", "0")
    or "0"
)
TARGET_THREAD_NAME = os.getenv("NIXE_PHASH_SOURCE_THREAD_NAME", "imagephising").lower()
IMAGE_EXTS = (".png",".jpg",".jpeg",".webp",".gif",".bmp",".tif",".tiff",".heic",".heif")

NOTIFY_THREAD = bool(int(os.getenv("PHISH_NOTIFY_THREAD", "0")))
LOG_TTL_SECONDS = int(os.getenv("PHISH_LOG_TTL", "0"))
LIMIT_MSGS = int(os.getenv("PHISH_AUTO_RESEED_LIMIT", "2000"))
AUGMENT = bool(int(os.getenv("PHASH_AUGMENT_REGISTER", "1")))
MAX_FRAMES = int(os.getenv("PHASH_MAX_FRAMES", "6"))
AUG_PER = int(os.getenv("PHASH_AUGMENT_PER_FRAME", "5"))
TILE_GRID = int(os.getenv("TILE_GRID", "3"))
ENABLE = os.getenv("NIXE_ENABLE_HASH_PORT", "1") == "1"

def _render_db(phashes, dhashes=None, tiles=None):
    data = {"phash": phashes or []}
    if dhashes: data["dhash"] = dhashes
    if tiles:   data["tphash"] = tiles
    body = json.dumps(data, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return f"{PHASH_DB_MARKER}\n```json\n{body}\n```"

def _extract_hashes_from_json_msg(msg: discord.Message):
    if not msg or not msg.content:
        return [], [], []
    s = msg.content
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            obj = json.loads(s[i:j+1])
            arr_p = obj.get("phash", []) or []
            arr_d = obj.get("dhash", []) or []
            arr_t = obj.get("tphash", []) or []
            P = [str(x).strip() for x in arr_p if str(x).strip()]
            D = [str(x).strip() for x in arr_d if str(x).strip()]
            T = [str(x).strip() for x in arr_t if str(x).strip()]
            return P, D, T
        except Exception:
            return [], [], []
    return [], [], []

class PhashAutoReseedPort(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._ran_guilds = set()
        self.auto_task.start()

    def cog_unload(self):
        try: self.auto_task.cancel()
        except Exception: pass

    @tasks.loop(count=1)
    async def auto_task(self):
        if not ENABLE:
            return
        await self.bot.wait_until_ready()
        await asyncio.sleep(3)
        for g in list(self.bot.guilds):
            if g.id in self._ran_guilds:
                continue
            try:
                await self._process_guild(g)
                self._ran_guilds.add(g.id)
            except Exception:
                continue


    async def _process_guild(self, guild: discord.Guild):
        target_thread: discord.Thread | None = None

        # Prefer explicit thread ID from env (PHASH_IMAGEPHISH_THREAD_ID / NIXE_PHASH_SOURCE_THREAD_ID)
        if TARGET_THREAD_ID:
            try:
                ch = guild.get_thread(TARGET_THREAD_ID) or guild.get_channel(TARGET_THREAD_ID)
            except Exception:
                ch = None
            if isinstance(ch, discord.Thread):
                target_thread = ch

        # Fallback to name-based lookup if ID is not set or not found
        if not target_thread:
            target_thread = next(
                (th for th in guild.threads if (th.name or "").lower() == TARGET_THREAD_NAME),
                None,
            )
        if not target_thread:
            return
        parent = getattr(target_thread, "parent", None)
        if not parent:
            return

        db_msg: discord.Message | None = None

        # Prefer a specific DB message ID if configured, so existing board entries are preserved
        if DB_MSG_ID:
            try:
                m = await parent.fetch_message(DB_MSG_ID)
                if m and m.author.id == self.bot.user.id and PHASH_DB_MARKER in (m.content or ""):
                    db_msg = m
            except Exception:
                db_msg = None

        # Fallback: scan recent history to find an existing DB message
        if not db_msg:
            try:
                async for m in parent.history(limit=50):
                    if m.author.id == self.bot.user.id and PHASH_DB_MARKER in (m.content or ""):
                        db_msg = m
                        break
            except Exception:
                db_msg = None

        all_p, all_d, all_t = [], [], []
        scanned_msgs = scanned_atts = 0
        async for m in target_thread.history(limit=LIMIT_MSGS, oldest_first=True):
            scanned_msgs += 1
            if not m.attachments:
                continue
            for att in m.attachments:
                name = (att.filename or "").lower()
                if not any(name.endswith(ext) for ext in IMAGE_EXTS):
                    continue
                raw = await att.read()
                if not raw:
                    continue
                scanned_atts += 1

                hs = img_hashing.phash_list_from_bytes(
                    raw,
                    max_frames=MAX_FRAMES,
                    augment=AUGMENT,
                    augment_per_frame=AUG_PER,
                )
                if hs:
                    all_p.extend(hs)

                dhf = getattr(img_hashing, "dhash_list_from_bytes", None)
                if dhf:
                    ds = dhf(
                        raw,
                        max_frames=MAX_FRAMES,
                        augment=AUGMENT,
                        augment_per_frame=AUG_PER,
                    )
                    if ds:
                        all_d.extend(ds)

                tfunc = getattr(img_hashing, "tile_phash_list_from_bytes", None)
                if tfunc:
                    ts = tfunc(
                        raw,
                        grid=TILE_GRID,
                        max_frames=4,
                        augment=AUGMENT,
                        augment_per_frame=0,
                    )
                    if ts:
                        all_t.extend(ts)

                if (scanned_atts % 25) == 0:
                    await asyncio.sleep(1)

        existing_p, existing_d, existing_t = ([], [], [])
        if db_msg:
            existing_p, existing_d, existing_t = _extract_hashes_from_json_msg(db_msg)

        sp, sd, st = set(existing_p), set(existing_d), set(existing_t)
        for h in all_p:
            if h not in sp:
                existing_p.append(h)
                sp.add(h)
        for h in all_d:
            if h not in sd:
                existing_d.append(h)
                sd.add(h)
        for t in all_t:
            if t not in st:
                existing_t.append(t)
                st.add(t)

        content = _render_db(existing_p, existing_d, existing_t)

        if db_msg:
            try:
                await db_msg.edit(content=content)
            except Exception:
                pass
        else:
            try:
                db_msg = await parent.send(content)
            except Exception:
                db_msg = None

        if NOTIFY_THREAD:
            try:
                emb = discord.Embed(
                    title="Auto reseed selesai",
                    description=f"Thread: {target_thread.mention}\nScanned: {scanned_msgs} msgs / {scanned_atts} attachments",
                    colour=0x00B894,
                )
                emb.add_field(name="Total pHash", value=str(len(existing_p)), inline=True)
                emb.add_field(name="Total dHash", value=str(len(existing_d)), inline=True)
                m = await parent.send(embed=emb, allowed_mentions=AllowedMentions.none())
                if LOG_TTL_SECONDS > 0:
                    await asyncio.sleep(LOG_TTL_SECONDS)
                    await safe_delete(m, label="delete")
            except Exception:
                pass

async def setup(bot: commands.Bot):
    await bot.add_cog(PhashAutoReseedPort(bot))
def legacy_setup(bot: commands.Bot):
    bot.add_cog(PhashAutoReseedPort(bot))
