
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, logging, json, asyncio, io
from typing import Set, List, Tuple, Optional

import discord
from discord.ext import commands

from nixe.helpers.img_hashing import phash_list_from_bytes
GUARD_ALL = (os.getenv("PHISH_GUARD_ALL_CHANNELS","1").strip().lower() in ("1","true","yes","on"))

from nixe.helpers.phash_tools import hamming
from nixe.state_runtime import get_phash_ids
from nixe.helpers.ban_utils import emit_phish_detected
from nixe.helpers.once import once_sync as _once

log = logging.getLogger("nixe.cogs.phash_phish_guard")

try:
    from PIL import Image as _PIL_Image
except Exception:
    _PIL_Image = None


def _transcode_to_png_bytes(raw: bytes) -> bytes:
    """Best-effort decode+re-encode to PNG.

    Rationale: some WEBP payloads (esp. animated / metadata-heavy) can produce
    unstable perceptual hashes. Transcoding helps confirm true matches and
    reduce false positives.
    """
    if not raw or _PIL_Image is None:
        return b""
    try:
        im = _PIL_Image.open(io.BytesIO(raw))
        # Pick first frame if animated
        try:
            im.seek(0)
        except Exception:
            pass
        im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue() or b""
    except Exception:
        return b""

def _env_int(name: str, default: int = 0) -> int:
    try:
        v = os.getenv(name)
        if v is None or v == "":
            return default
        return int(v)
    except Exception:
    # silently fall back to default
        return default

def _env_set(name: str) -> Set[int]:
    out: Set[int] = set()
    raw = os.getenv(name) or ""
    for tok in raw.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except Exception:
            continue
    return out

def _load_phash_list_from_content(content: str) -> Tuple[Set[int], Set[int]]:
    """Parse pinned message content into two sets:
    - confirmed pHash tokens (plain hex strings)
    - autolearn pHash tokens (prefixed with "a:")
    """
    if not content:
        return set(), set()

    text = content.strip()
    data: Optional[object] = None

    try:
        data = json.loads(text)
    except Exception:
        data = None

    if data is None and "```" in text:
        try:
            s0 = text.find("```")
            e0 = text.rfind("```")
            if s0 != -1 and e0 != -1 and e0 > s0:
                inner = text[s0 + 3 : e0].strip()
                lines = inner.splitlines()
                if lines and not lines[0].lstrip().startswith("{"):
                    inner = "\n".join(lines[1:])
                data = json.loads(inner)
        except Exception:
            data = None

    if data is None:
        start_brace = text.find("{")
        end_brace = text.rfind("}")
        if start_brace == -1 or end_brace == -1 or end_brace <= start_brace:
            return set(), set()
        inner = text[start_brace : end_brace + 1]
        try:
            data = json.loads(inner)
        except Exception:
            return set(), set()

    seq = None
    if isinstance(data, dict):
        seq = data.get("phash") or data.get("hashes") or data.get("items") or []
    elif isinstance(data, list):
        seq = data
    else:
        seq = []

    confirmed: Set[int] = set()
    autolearn: Set[int] = set()

    for item in (seq or []):
        try:
            raw = str(item).strip()
            if not raw:
                continue
            is_auto = False
            if raw.lower().startswith("a:"):
                is_auto = True
                raw = raw[2:].strip()
            if raw.lower().startswith("0x"):
                raw = raw[2:].strip()
            hv = int(raw, 16)
            (autolearn if is_auto else confirmed).add(hv)
        except Exception:
            continue

    return confirmed, autolearn



class PhashPhishGuard(commands.Cog):
    """Lightweight pHash-based phishing guard.

    - Loads pHash blacklist once from DB thread/message (runtime_env: PHASH_DB_THREAD_ID / PHASH_DB_MESSAGE_ID).
    - Only checks image attachments (png/jpg/jpeg/webp/gif).
    - On match (Hamming <= PHASH_MATCH_DELETE_MAX_BITS) it emits `nixe_phish_detected`,
      so existing phish_ban_embed pipeline handles log + auto-ban.
    - Never deletes anything inside the imagephish/db threads themselves.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._hashes_confirmed: Set[int] = set()
        self._hashes_autolearn: Set[int] = set()
        self.bits_max: int = _env_int("PHASH_MATCH_DELETE_MAX_BITS", 12)
        # WEBP pHash matching must be strict to avoid false positives.
        self.bits_max_webp: int = _env_int("PHISH_PHASH_WEBP_MAX_BITS", min(self.bits_max, 6))
        # Only enforce pHash-ban for WEBP under this size.
        self.max_bytes_webp: int = _env_int("PHISH_WEBP_PHASH_MAX_BYTES", 1048576)
        self.max_bytes_other: int = _env_int("PHISH_PHASH_MAX_BYTES_OTHER", 3145728)
        # If set (>0), PHISH_PHASH_MAX_BYTES overrides both caps.
        self.max_bytes_override: int = _env_int("PHISH_PHASH_MAX_BYTES", 0)
        self.seen_ttl_sec: int = _env_int("PHISH_PHASH_SEEN_TTL_SEC", 900)
        self.guard_ids: Set[int] = _env_set("LPG_GUARD_CHANNELS")
        # Channels/threads where pHash phishing guard must NEVER act
        # (e.g. mod rooms, phash boards, forums, or all-thread environments).
        self.skip_ids: Set[int] = _env_set("PHISH_SKIP_CHANNELS")
        # Also respect PHASH_MATCH_SKIP_CHANNELS for compatibility with LPG/pHash boards
        match_skip = _env_set("PHASH_MATCH_SKIP_CHANNELS")
        if match_skip:
            self.skip_ids |= match_skip
        self.safe_threads: Set[int] = {
            _env_int("PHASH_IMAGEPHISH_THREAD_ID", 0),
            _env_int("PHASH_DB_THREAD_ID", 0),
            _env_int("PHASH_SOURCE_THREAD_ID", 0),
            _env_int("PHASH_IMPORT_SOURCE_THREAD_ID", 0),
        }
        self.log_chan_id: int = _env_int("PHISH_LOG_CHAN_ID", _env_int("NIXE_PHISH_LOG_CHAN_ID", 0))
        # lazy bootstrap
        self._bootstrap_task = asyncio.create_task(self._bootstrap())
        log.info(
            "[phash-phish] init bits_max=%s guards=%s skip=%s safe=%s",
            self.bits_max,
            sorted(self.guard_ids),
            sorted(self.skip_ids),
            sorted(self.safe_threads),
        )

    async def _bootstrap(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self._refresh_hashes()
        except Exception as e:
            log.warning("[phash-phish] initial load failed: %r", e)

    
    async def _fetch_db_message(self) -> Tuple[Optional[discord.abc.Messageable], Optional[discord.Message]]:
        """
        Resolve the pHash DB message for phishing guard.

        Resolution order (additive, no breaking change):
        1) PHISH_PHASH_DB_THREAD_ID / PHISH_PHASH_DB_MESSAGE_ID (phish-specific override)
        2) Runtime ids published via state_runtime.get_phash_ids()
        3) PHASH_DB_THREAD_ID / PHASH_DB_MESSAGE_ID (shared DB fallback)
        4) PHISH_LOG_CHAN_ID / NIXE_PHISH_LOG_CHAN_ID as channel fallback when only message id is known.
        """
        # 1) Explicit phish-specific overrides (if provided)
        tid = _env_int("PHISH_PHASH_DB_THREAD_ID", 0)
        mid = _env_int("PHISH_PHASH_DB_MESSAGE_ID", 0)

        # 2) Runtime ids published by other cogs (e.g. phash board / LPG DB)
        if not tid or not mid:
            rt_tid, rt_mid = get_phash_ids()
            if not tid and rt_tid:
                tid = rt_tid
            if not mid and rt_mid:
                mid = rt_mid

                # 2.5) If only the imagephish DB thread id is provided, treat it as the DB thread.
        if not tid:
            tid = _env_int("PHASH_IMAGEPHISH_THREAD_ID", 0)

# 3) Shared DB fallback from generic PHASH_DB_* vars
        if not tid:
            tid = _env_int("PHASH_DB_THREAD_ID", 0)
        if not mid:
            mid = _env_int("PHASH_DB_MESSAGE_ID", 0)

        if not mid:
            # Thread-only mode: allow _refresh_hashes() to scan pins/history in the thread.
            if tid:
                try:
                    channel = self.bot.get_channel(tid) or await self.bot.fetch_channel(tid)
                    return channel, None
                except Exception:
                    return None, None
            return None, None

        channel: Optional[discord.abc.Messageable] = None
        msg: Optional[discord.Message] = None
        try:
            if tid:
                channel = self.bot.get_channel(tid) or await self.bot.fetch_channel(tid)
            # last resort: use phish log channel as a parent when only message id is known
            if channel is None and self.log_chan_id:
                channel = self.bot.get_channel(self.log_chan_id) or await self.bot.fetch_channel(self.log_chan_id)
            if channel:
                msg = await channel.fetch_message(mid)
        except Exception as e:
            log.warning("[phash-phish] fetch db failed: %r", e)
        return channel, msg

    async def _refresh_hashes(self) -> None:
        ch, msg = await self._fetch_db_message()
        msgs: list[discord.Message] = []

        if msg is not None:
            msgs.append(msg)

        # If a thread/channel is available, also parse pinned messages (preferred for DB boards)
        if ch is not None:
            try:
                pins = await ch.pins()  # type: ignore[attr-defined]
                if isinstance(pins, list) and pins:
                    msgs.extend([m for m in pins if m is not None])
            except Exception:
                pass

            # As a fallback, scan recent messages in the thread/channel for DB JSON.
            # (Keep this bounded to avoid rate limits.)
            try:
                async for m2 in ch.history(limit=80):  # type: ignore[attr-defined]
                    c = getattr(m2, "content", "") or ""
                    if not c:
                        continue
                    if ("\"phash\"" in c) or ("NIXE_PHASH_DB" in c) or ("[\"phash\"" in c):
                        msgs.append(m2)
            except Exception:
                pass

        merged_confirmed: Set[int] = set()
        merged_autolearn: Set[int] = set()
        for m2 in msgs:
            try:
                cset, aset = _load_phash_list_from_content(getattr(m2, "content", "") or "")
                merged_confirmed |= cset
                merged_autolearn |= aset
            except Exception:
                continue

        if not merged_confirmed and not merged_autolearn:
            log.warning(
                "[phash-phish] db parse yields 0 hashes (thread=%s)",
                getattr(ch, "id", 0) if ch else 0,
            )
            return

        self._hashes_confirmed = merged_confirmed
        self._hashes_autolearn = merged_autolearn
        log.warning(
            "[phash-phish] loaded confirmed=%d autolearn=%d (thread=%s)",
            len(merged_confirmed),
            len(merged_autolearn),
            getattr(ch, "id", 0) if ch else 0,
        )

    def _should_guard_channel(self, ch: discord.abc.GuildChannel | discord.Thread) -> bool:
        cid = int(getattr(ch, "id", 0) or 0)
        pid = int(getattr(ch, "parent_id", 0) or 0)
        parent = getattr(ch, "parent", None)
        try:
            from discord import ForumChannel
        except Exception:
            ForumChannel = None
        if ForumChannel and (isinstance(ch, ForumChannel) or isinstance(parent, ForumChannel)):
            return False
        ctype = getattr(ch, "type", None)
        ptype = getattr(parent, "type", None)
        if any("forum" in str(t).lower() for t in (ctype, ptype)):
            return False
        if not cid:
            return False
        # Never guard inside the dedicated imagephish/db/source threads.
        if cid in self.safe_threads or (pid and pid in self.safe_threads):
            return False
        # Respect PHISH_SKIP_CHANNELS / PHASH_MATCH_SKIP_CHANNELS.
        if cid in self.skip_ids or (pid and pid in self.skip_ids):
            return False
        if GUARD_ALL:
            # Guard all channels/threads except safe/skip ones.
            return True
        if not self.guard_ids:
            # Guard all top-level channels (except safe/skip ones) by default.
            return pid == 0
        return cid in self.guard_ids or (pid and pid in self.guard_ids)

    async def _scan_message(self, m: discord.Message) -> None:
        if not self._hashes_confirmed and not self._hashes_autolearn:
            return
        if m.author.bot:
            return
        ch = getattr(m, "channel", None)
        if ch is None:
            return
        if not self._should_guard_channel(ch):
            return

        # Collect candidate image attachments (filtered by PHISH_PHASH_EXTS).
        images: List[Tuple[bytes, bool]] = []  # (raw_bytes, is_webp)
        exts_env = os.getenv("PHISH_PHASH_EXTS", "webp,png,jpg,jpeg,gif").lower()
        allowed_exts = tuple(sorted({('.' + e.strip().lstrip('.')) for e in exts_env.split(',') if e.strip()}))
        ct_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif'}
        allowed_cts = tuple(sorted({ct_map.get(ext) for ext in allowed_exts if ct_map.get(ext)}))
        if not allowed_exts:
            allowed_exts = ('.webp', '.png')
        if not allowed_cts:
            allowed_cts = ('image/webp', 'image/png')
        for a in getattr(m, "attachments", []) or []:
            name = (getattr(a, "filename", "") or "").lower()
            ct = (getattr(a, "content_type", "") or "").lower()
            looks_image = (ct in allowed_cts) or any(name.endswith(ext) for ext in allowed_exts)
            if not looks_image:
                continue

            # Size gate: phishing pHash scan runs ONLY for images <= max_bytes.
            # Larger images are left for LPG / other pipelines.
            try:
                size = int(getattr(a, "size", 0) or 0)
            except Exception:
                size = 0
            cap_pre = self.max_bytes_override if self.max_bytes_override > 0 else self.max_bytes_other
            if size and size > cap_pre:
                continue

            url = getattr(a, "url", None) or ""
            if url and not _once(f"phash:any:{url}", ttl=self.seen_ttl_sec):
                continue

            try:
                b = await a.read()
                if not b:
                    continue
                # Hard enforce after download (cap depends on detected format).
                # Robust WEBP detection even when the filename is misleading.
                is_webp = (ct == "image/webp") or name.endswith(".webp") or (len(b) > 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP")
                cap_eff = self.max_bytes_override if self.max_bytes_override > 0 else (self.max_bytes_webp if is_webp else self.max_bytes_other)
                if len(b) > cap_eff:
                    continue
                images.append((b, bool(is_webp)))
            except Exception:
                continue

        if not images:
            return


        if not images:
            return

        # Compare each local phash to blacklist.
        for raw, is_webp in images:
            bits_max_eff = min(self.bits_max, self.bits_max_webp) if is_webp else self.bits_max
            hashes = phash_list_from_bytes(raw, max_frames=4)
            for s in hashes:
                try:
                    hv = int(str(s), 16)
                except Exception:
                    continue

                hit_provider: Optional[str] = None
                hit_bits: int = bits_max_eff

                # 1) Confirmed pHash tokens (hard hit)
                for ref in self._hashes_confirmed:
                    if hamming(hv, ref) <= bits_max_eff:
                        # Extra verification for WEBP to reduce false positives.
                        if is_webp:
                            png = _transcode_to_png_bytes(raw)
                            if png:
                                hashes2 = phash_list_from_bytes(png, max_frames=4)
                                confirmed = False
                                for s2 in hashes2:
                                    try:
                                        hv2 = int(str(s2), 16)
                                    except Exception:
                                        continue
                                    if hamming(hv2, ref) <= bits_max_eff:
                                        confirmed = True
                                        break
                                if not confirmed:
                                    continue
                            else:
                                if hamming(hv, ref) > max(0, bits_max_eff - 2):
                                    continue

                        hit_provider = "phash"
                        hit_bits = bits_max_eff
                        break

                # 2) Autolearn / probationary tokens (stricter match; NEVER auto-ban)
                if hit_provider is None and self._hashes_autolearn:
                    bits_auto = max(0, bits_max_eff - 2)
                    for ref in self._hashes_autolearn:
                        if hamming(hv, ref) <= bits_auto:
                            if is_webp:
                                png = _transcode_to_png_bytes(raw)
                                if png:
                                    hashes2 = phash_list_from_bytes(png, max_frames=4)
                                    confirmed = False
                                    for s2 in hashes2:
                                        try:
                                            hv2 = int(str(s2), 16)
                                        except Exception:
                                            continue
                                        if hamming(hv2, ref) <= bits_auto:
                                            confirmed = True
                                            break
                                    if not confirmed:
                                        continue
                                else:
                                    if hamming(hv, ref) > max(0, bits_auto - 2):
                                        continue

                            hit_provider = "phash-autolearn"
                            hit_bits = bits_auto
                            break

                if hit_provider is None:
                    continue

                reason = f"{hit_provider}≤{hit_bits} hv={hv:x}"
                ev_urls: List[str] = []
                for att in getattr(m, "attachments", []) or []:
                    url = getattr(att, "url", None)
                    if url:
                        ev_urls.append(url)

                details = {
                    "score": 1.0 if hit_provider == "phash" else 0.95,
                    "provider": hit_provider,
                    "reason": reason,
                    "kind": "image",
                    "phash_hv": f"{hv:x}",
                    "phash_bits": hit_bits,
                }
                emit_phish_detected(self.bot, m, details, ev_urls)
                log.warning(
                    "[phash-phish] HIT provider=%s mid=%s user=%s ch=%s hv=%s",
                    hit_provider,
                    getattr(m, "id", "?"),
                    getattr(getattr(m, "author", None), "id", "?"),
                    getattr(ch, "id", "?"),
                    f"{hv:x}",
                )
                return
    @commands.Cog.listener("on_message")
    async def on_message(self, m: discord.Message) -> None:
        try:
            await self._scan_message(m)
        except Exception as e:
            log.debug("[phash-phish] err: %r", e)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PhashPhishGuard(bot))
