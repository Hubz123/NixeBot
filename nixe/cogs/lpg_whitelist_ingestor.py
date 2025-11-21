
from __future__ import annotations
import os, json, logging, asyncio
from typing import Any, Dict, List
import discord
from discord.ext import commands, tasks

log = logging.getLogger("nixe.cogs.lpg_whitelist_ingestor")

def _parse_int(val: str | int | None, default: int = 0) -> int:
    try:
        return int(val) if val is not None else default
    except Exception:
        return default

def _is_image(att: discord.Attachment) -> bool:
    ct = (getattr(att, "content_type", None) or "").lower()
    if ct.startswith("image/"):
        return True
    name = (getattr(att, "filename", "") or "").lower()
    return name.endswith((".png",".jpg",".jpeg",".webp",".bmp",".gif"))

class LPGWhitelistIngestor(commands.Cog):
    """Ingestor untuk thread Whitelist Lucky Pull (FP).
    - Read-only terhadap runtime_env.json (format tidak diubah).
    - Fail-soft: warning alih-alih crash.
    - Simpan metadata attachment image ke JSON lokal untuk dipakai modul lain.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = (os.getenv("LPG_WHITELIST_ENABLE") or "1") == "1"
        self.thread_id = _parse_int(os.getenv("LPG_WHITELIST_THREAD_ID"))
        # Fallback: cari via parent+name jika ID tidak ada/invalid
        self.parent_id = _parse_int(os.getenv("LPG_WHITELIST_PARENT_CHANNEL_ID"))
        if not self.parent_id:
            self.parent_id = _parse_int(os.getenv("LPG_NEG_PARENT_CHANNEL_ID"))
        self.thread_name = os.getenv("LPG_WHITELIST_THREAD_NAME") or "Whitelist LPG (FP)"
        self.db_path = os.getenv("LPG_WHITELIST_DB_PATH") or "nixe/data/lpg_whitelist.json"
        try:
            self.scan_limit = int(os.getenv("LPG_WHITELIST_SCAN_LIMIT") or "400")
        except Exception:
            self.scan_limit = 400
        try:
            self.interval_sec = int(os.getenv("LPG_WHITELIST_INTERVAL_SEC") or "900")
        except Exception:
            self.interval_sec = 900

        # internal state
        self._ingesting = asyncio.Lock()
        self._last_count = 0

        log.info("[lpg-wl-ingest] enabled=%s thread_id=%s parent_id=%s name=%s db=%s limit=%s interval=%ss",
                 self.enabled, self.thread_id, self.parent_id, self.thread_name, self.db_path, self.scan_limit, self.interval_sec)

    def _ensure_dir(self, path: str) -> None:
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
        except Exception:
            pass

    def _load_db(self) -> Dict[str, Any]:
        """Load persisted whitelist DB.

        Supports legacy formats (list-only) and self-heals corrupt shapes.
        Always returns a dict with key 'attachments' -> list.
        """
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # Legacy: file was just a list of entries
            if isinstance(raw, list):
                log.warning("[lpg-wl-ingest] legacy db format (list) detected; normalizing to dict")
                return {"attachments": raw}
            if isinstance(raw, dict):
                # Some older shapes may store the list under a different key
                if "attachments" not in raw:
                    for k in ("items", "data", "whitelist", "images"):
                        if k in raw and isinstance(raw[k], list):
                            log.warning("[lpg-wl-ingest] db missing 'attachments'; using '%s' list and normalizing", k)
                            return {"attachments": raw[k]}
                    raw["attachments"] = []
                # Self-heal if attachments is not a list
                if not isinstance(raw.get("attachments"), list):
                    log.warning("[lpg-wl-ingest] db 'attachments' is not a list; resetting")
                    raw["attachments"] = []
                return raw
            # Unknown type
            log.warning("[lpg-wl-ingest] db has unexpected type %s; resetting", type(raw).__name__)
            return {"attachments": []}
        except Exception:
            return {"attachments": []}

    def _save_db(self, data: Dict[str, Any]) -> None:
(self, data: Dict[str, Any]) -> None:
        try:
            self._ensure_dir(self.db_path)
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("[lpg-wl-ingest] save failed: %r", e)

    async def _resolve_thread(self, guild: discord.Guild) -> discord.Thread | None:
        # 1) direct by id
        if self.thread_id:
            try:
                thr = await self.bot.fetch_channel(self.thread_id)
                if isinstance(thr, discord.Thread):
                    return thr
            except Exception as e:
                log.debug("[lpg-wl-ingest] fetch by thread_id failed: %r", e)

        # 2) search under parent by name
        try:
            parent = None
            if self.parent_id:
                parent = guild.get_channel(self.parent_id) or await self.bot.fetch_channel(self.parent_id)
            # gather active + archived threads to search
            threads: List[discord.Thread] = []
            for th in guild.threads:
                threads.append(th)
            if isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
                async for th in parent.archived_threads(limit=100):
                    threads.append(th)
            for th in threads:
                try:
                    if isinstance(th, discord.Thread) and (th.name or "").strip().lower() == self.thread_name.strip().lower():
                        return th
                except Exception:
                    continue
        except Exception as e:
            log.debug("[lpg-wl-ingest] resolve by parent+name failed: %r", e)

        return None

    async def _ingest_once(self) -> int:
        if not self.enabled:
            return 0
        if not self.bot.guilds:
            return 0
        guild = self.bot.guilds[0]
        thr = await self._resolve_thread(guild)
        if not thr:
            log.warning("[lpg-wl-ingest] whitelist thread not found (id=%s, parent=%s, name=%s)",
                        self.thread_id, self.parent_id, self.thread_name)
            return 0

        data = self._load_db()
        if isinstance(data, list):
            data = {"attachments": data}
        items: List[Dict[str, Any]] = (data or {}).get("attachments", [])
        seen = {(it.get("message_id"), it.get("attachment_id")) for it in items}
        added = 0

        try:
            async for msg in thr.history(limit=self.scan_limit, oldest_first=True):
                for att in (msg.attachments or []):
                    if not _is_image(att):
                        continue
                    key = (msg.id, att.id)
                    if key in seen:
                        continue
                    entry = {
                        "message_id": msg.id,
                        "attachment_id": att.id,
                        "filename": att.filename,
                        "size": att.size,
                        "content_type": att.content_type,
                        "url": att.url,
                        "proxy_url": att.proxy_url,
                        "created_at": int(msg.created_at.timestamp()) if msg.created_at else None,
                    }
                    items.append(entry)
                    seen.add(key)
                    added += 1
        except Exception as e:
            log.warning("[lpg-wl-ingest] scan failed: %r", e)

        data["attachments"] = items
        self._save_db(data)
        total = len(items)
        if added or total != self._last_count:
            log.info("[lpg-wl-ingest] stored=%s (+%s) path=%s", total, added, self.db_path)
        self._last_count = total
        return total

    @tasks.loop(count=1)
    async def _bootstrap(self):
        # single-shot bootstrap; no interval to avoid re-launch overlap
        await asyncio.sleep(1.0)
        async with self._ingesting:
            await self._ingest_once()
        # launch periodic safely
        if self.interval_sec > 0:
            try:
                self._periodic.change_interval(seconds=float(self.interval_sec))
            except Exception:
                pass
            if not self._periodic.is_running():
                self._periodic.start()
            else:
                log.info("[lpg-wl-ingest] periodic already running; interval=%ss kept", self.interval_sec)

    @tasks.loop(seconds=3600.0)
    async def _periodic(self):
        async with self._ingesting:
            await self._ingest_once()

    @_bootstrap.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    @_periodic.before_loop
    async def _wait_ready2(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener("on_ready")
    async def on_ready(self):
        if not self.enabled:
            log.info("[lpg-wl-ingest] disabled via LPG_WHITELIST_ENABLE")
            return
        if not self._bootstrap.is_running():
            self._bootstrap.start()
        else:
            log.debug("[lpg-wl-ingest] bootstrap already running")

async def setup(bot: commands.Bot):
    await bot.add_cog(LPGWhitelistIngestor(bot))
