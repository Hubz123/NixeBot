from __future__ import annotations
import os, logging, asyncio
from typing import Optional, List, Tuple, Any
import discord
from discord.ext import commands
from nixe.helpers.persona_loader import load_persona, pick_line
from nixe.helpers.persona_gate import should_run_persona
try:
    from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes
except Exception:
    classify_lucky_pull_bytes = None

log = logging.getLogger("nixe.cogs.a00_lpg_thread_bridge_guard")


# -- simple pHash + helpers (minimal) --
from io import BytesIO
import json
from pathlib import Path
try:
    from PIL import Image
except Exception:
    Image = None
try:
    import numpy as np
except Exception:
    np = None


def _dct2(a):
    return np.real(np.fft.fft2(a)) if np is not None else None


def _phash64_bytes(img_bytes: bytes) -> Optional[int]:
    """Compute 64-bit perceptual hash from raw image bytes.
    Returns None on any failure but logs the reason for easier debugging.
    """
    if Image is None or np is None:
        log.warning(
            "[lpg-thread-bridge] pHash disabled (Image=%r, np=%r)",
            Image,
            np,
        )
        return None
    try:
        im = Image.open(BytesIO(img_bytes)).convert("L").resize((32, 32))
        arr = np.asarray(im, dtype=np.float32)
        d = _dct2(arr)[:8, :8]
        med = float(np.median(d[1:, 1:]))
        bits = (d[1:, 1:] > med).astype(np.uint8).flatten()
        val = 0
        for b in bits:
            val = (val << 1) | int(b)
        return int(val)
    except Exception as e:
        log.warning("[lpg-thread-bridge] _phash64_bytes failed: %r", e)
        return None


async def _post_status_embed(
    bot, *, title: str, fields: List[Tuple[str, str, bool]], color: int = 0x2B6CB0
):
    tid = None
    for k in ("LPG_STATUS_THREAD_ID", "NIXE_STATUS_THREAD_ID"):
        v = os.getenv(k, "")
        if v.isdigit():
            tid = int(v)
            break
    if tid is None:
        tid = 1435924665615908965
    try:
        ch = bot.get_channel(tid) or await bot.fetch_channel(tid)
        if ch:
            import discord

            emb = discord.Embed(title=title, color=color)
            for name, value, inline in fields:
                emb.add_field(name=name, value=value, inline=inline)
            await ch.send(embed=emb)
    except Exception:
        pass


def _cache_path(thread_id: int) -> Path:
    base = Path(os.getenv("LPG_CACHE_DIR", "data/phash_cache"))
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{thread_id}.json"


def _load_cache(thread_id: int) -> dict:
    p = _cache_path(thread_id)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(thread_id: int, data: dict) -> None:
    p = _cache_path(thread_id)
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _parse_ids(val: str) -> list[int]:
    if not val:
        return []
    out = []
    for part in str(val).replace(";", ",").split(","):
        s = part.strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except Exception:
            pass
    return out


def _cid(ch) -> int:
    try:
        return int(getattr(ch, "id", 0) or 0)
    except Exception:
        return 0


def _pid(ch) -> int:
    try:
        return int(getattr(ch, "parent_id", 0) or 0)
    except Exception:
        return 0


def _norm_tone(t: str) -> str:
    t = (t or "").lower().strip()
    if t in ("soft", "agro", "sharp"):
        return t
    if t in ("harsh", "hard"):
        return "agro"
    if t in ("gentle", "calm", "auto", ""):
        return "soft"
    return "soft"


class LPGThreadBridgeGuard(commands.Cog):
    """Lucky Pull guard (thread-aware) — fully env-aligned.
    - Only deletes when image is classified as Lucky (Gemini) unless LPG_REQUIRE_CLASSIFY=0.
    - Persona strictly follows LPG_PERSONA_* / PERSONA_* in runtime_env.json.
    - Wiring keys read-only; format preserved.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = os.getenv("LPG_BRIDGE_ENABLE", "1") == "1"
        self.strict = (
            os.getenv("LPG_STRICT_ON_GUARD")
            or os.getenv("LUCKYPULL_STRICT_ON_GUARD")
            or os.getenv("STRICT_ON_GUARD")
            or "1"
        ) == "1"
        self.redirect_id = int(
            os.getenv("LPG_REDIRECT_CHANNEL_ID")
            or os.getenv("LUCKYPULL_REDIRECT_CHANNEL_ID")
            or "0"
        )
        self.guard_ids = set(
            _parse_ids(
                os.getenv("LPG_GUARD_CHANNELS", "")
                or os.getenv("LUCKYPULL_GUARD_CHANNELS", "")
            )
        )
        self.timeout = float(
            os.getenv("LPG_TIMEOUT_SEC", os.getenv("LUCKYPULL_TIMEOUT_SEC", "10"))
        )
        self.thr = float(
            os.getenv("LPG_GEMINI_THRESHOLD")
            or os.getenv("GEMINI_LUCKY_THRESHOLD", "0.85")
        )
        self.require_classify = (os.getenv("LPG_REQUIRE_CLASSIFY") or "1") == "1"
        try:
            self.max_bytes = int(os.getenv("LPG_MAX_BYTES") or 8_000_000)
        except Exception:
            self.max_bytes = 8_000_000

        # Persona
        self.persona_enable = (
            os.getenv("LPG_PERSONA_ENABLE") or os.getenv("PERSONA_ENABLE") or "1"
        ) == "1"
        self.persona_mode = (
            os.getenv("LPG_PERSONA_MODE") or os.getenv("PERSONA_MODE") or "yandere"
        )
        self.persona_tone = _norm_tone(
            os.getenv("LPG_PERSONA_TONE")
            or os.getenv("PERSONA_TONE")
            or os.getenv("PERSONA_GROUP")
            or "soft"
        )
        self.persona_reason = (
            os.getenv("LPG_PERSONA_REASON")
            or os.getenv("PERSONA_REASON")
            or "Tebaran Garam"
        )
        self.persona_context = (
            os.getenv("LPG_PERSONA_CONTEXT") or "lucky"
        ).strip().lower()
        try:
            self.persona_delete_after = float(
                os.getenv("LPG_PERSONA_DELETE_AFTER")
                or os.getenv("PERSONA_DELETE_AFTER")
                or "12"
            )
        except Exception:
            self.persona_delete_after = 12.0
        # Persona scoping from runtime_env
        self.persona_only_for = [
            s.strip().lower()
            for s in str(os.getenv("LPG_PERSONA_ONLY_FOR", "")).split(",")
            if s.strip()
        ]
        self.persona_allowed_providers = [
            s.strip().lower()
            for s in str(os.getenv("LPG_PERSONA_ALLOWED_PROVIDERS", "")).split(",")
            if s.strip()
        ]

        log.warning(
            "[lpg-thread-bridge] enabled=%s guards=%s redirect=%s thr=%.2f strict=%s timeout=%.1fs require_classify=%s | persona: enable=%s mode=%s tone=%s reason=%s only_for=%s allow_prov=%s",
            self.enabled,
            sorted(self.guard_ids),
            self.redirect_id,
            self.thr,
            self.strict,
            self.timeout,
            self.require_classify,
            self.persona_enable,
            self.persona_mode,
            self.persona_tone,
            self.persona_reason,
            self.persona_only_for,
            self.persona_allowed_providers,
        )

    def _in_guard(self, ch) -> bool:
        return (_cid(ch) in self.guard_ids) or (
            _pid(ch) in self.guard_ids and _pid(ch) != 0
        )

    def _is_image(self, att: discord.Attachment) -> bool:
        ct = (getattr(att, "content_type", None) or "").lower()
        if ct.startswith("image/"):
            return True
        name = (getattr(att, "filename", "") or "").lower()
        return name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"))

    async def _classify(self, message: discord.Message) -> tuple[bool, float, str, str]:
        """Classify image with resilient Gemini + BURST fallback.
        Returns: (lucky_ok, score, provider, reason)
        """
        if not classify_lucky_pull_bytes:
            return (False, 0.0, "none", "classifier_missing")

        imgs = [a for a in (message.attachments or []) if self._is_image(a)]
        if not imgs:
            return (False, 0.0, "none", "no_image")

        data: Optional[bytes] = None
        try:
            data = await imgs[0].read()
            if not data:
                return (False, 0.0, "none", "empty_bytes")
            if len(data) > self.max_bytes:
                data = data[: self.max_bytes]

            # primary path: gemini_bridge (may be monkeypatched by overlay)
            res = await asyncio.wait_for(
                classify_lucky_pull_bytes(data), timeout=self.timeout
            )

            ok: bool = False
            score: float = 0.0
            provider: str = "unknown"
            reason: str = ""

            if isinstance(res, tuple) and len(res) >= 4:
                ok, score, provider, reason = (
                    res[0],
                    float(res[1] or 0.0),
                    str(res[2]),
                    str(res[3]),
                )
            elif isinstance(res, dict):
                ok = bool(res.get("ok", False))
                score = float(res.get("score", 0.0))
                provider = str(res.get("provider", "unknown"))
                reason = str(res.get("reason", ""))

            provider = provider or "unknown"
            reason = reason or ""
            score = float(score or 0.0)

            # If Gemini gave a parse / no_result style error, escalate to BURST once.
            bad_reason = False
            rlow = reason.lower()
            if ("parse_error" in rlow) or ("none:no_result" in rlow) or (
                "classify_exception" in rlow
            ):
                bad_reason = True
            if not rlow and provider.startswith("gemini:") and (not ok) and score <= 0.0:
                bad_reason = True

            if bad_reason and provider.startswith("gemini:") and data:
                try:
                    try:
                        from nixe.helpers.gemini_lpg_burst import (
                            classify_lucky_pull_bytes_burst as _burst,
                        )
                    except Exception:
                        try:
                            from nixe.helpers.gemini_lpg_burst import (
                                classify_lucky_pull_bytes as _burst,
                            )
                        except Exception:
                            _burst = None
                    if _burst is not None:
                        fb_ms = int(os.getenv("LPG_GUARD_LASTCHANCE_MS", "1200"))
                        os.environ["LPG_BURST_TIMEOUT_MS"] = str(fb_ms)
                        bok, bscore, bvia, breason = await _burst(data)
                        bscore = float(bscore or 0.0)
                        return (
                            bool(bok and bscore >= self.thr),
                            bscore,
                            str(bvia or "gemini:lastchance"),
                            f"lastchance({breason})",
                        )
                except Exception as e:
                    log.debug(
                        "[lpg-thread-bridge] lastchance burst on-parse-error failed: %r",
                        e,
                    )

            verdict_ok = bool(ok and score >= self.thr)
            return (verdict_ok, score, provider, reason or "classified")

        except asyncio.TimeoutError:
            # Guard hard-timeout hit; do one last-chance BURST retry (short) to avoid false negatives
            if not data:
                return (False, 0.0, "timeout", "classify_timeout")
            try:
                try:
                    from nixe.helpers.gemini_lpg_burst import (
                        classify_lucky_pull_bytes_burst as _burst,
                    )
                except Exception:
                    try:
                        from nixe.helpers.gemini_lpg_burst import (
                            classify_lucky_pull_bytes as _burst,
                        )
                    except Exception:
                        _burst = None
                if _burst is not None:
                    fb_ms = int(os.getenv("LPG_GUARD_LASTCHANCE_MS", "1200"))
                    os.environ["LPG_BURST_TIMEOUT_MS"] = str(fb_ms)
                    ok, score, via, reason = await _burst(data)
                    score = float(score or 0.0)
                    verdict_ok = bool(ok and score >= self.thr)
                    return (
                        verdict_ok,
                        score,
                        str(via or "gemini:lastchance"),
                        f"lastchance({reason})",
                    )
            except Exception:
                pass
            return (False, 0.0, "timeout", "classify_timeout")
        except Exception as e:
            log.warning("[lpg-thread-bridge] classify error: %r", e)
            return (False, 0.0, "error", "classify_exception")

    async def _delete_redirect_persona(
        self,
        message: discord.Message,
        lucky: bool,
        score: float,
        provider: str,
        reason: str,
        provider_hint: Optional[str] = None,
    ):
        # Delete
        try:
            await message.delete()
            log.info(
                "[lpg-thread-bridge] message deleted | user=%s ch=%s",
                getattr(message.author, "id", None),
                _cid(message.channel),
            )
        except Exception as e:
            log.debug("[lpg-thread-bridge] delete failed: %r", e)
        # Redirect mention
        mention = None
        try:
            if self.redirect_id:
                ch = message.guild.get_channel(self.redirect_id) or await self.bot.fetch_channel(  # type: ignore[arg-type]
                    self.redirect_id
                )
                mention = ch.mention if ch else f"<#{self.redirect_id}>"
        except Exception as e:
            log.debug("[lpg-thread-bridge] redirect resolve failed: %r", e)
        # Persona
        text = None
        persona_ok = False
        try:
            ok_run, _pm = should_run_persona(
                {
                    "ok": bool(lucky),
                    "score": float(score or 0.0),
                    "kind": self.persona_context,
                    "provider": provider,
                    "reason": reason,
                }
            )
            if self.persona_enable and ok_run:
                if self.persona_only_for and self.persona_context not in self.persona_only_for:
                    raise RuntimeError("persona_context_not_allowed")
                if self.persona_allowed_providers:
                    prov = (provider_hint or "").lower()
                    if not any(prov.startswith(prefix) for prefix in self.persona_allowed_providers):
                        raise RuntimeError("persona_provider_not_allowed")
                pmode, pdata, ppath = load_persona()
                if not pdata:
                    raise RuntimeError("persona_data_empty")
                use_mode = self.persona_mode or pmode or "yandere"
                text = pick_line(
                    pdata,
                    mode=use_mode,
                    tone=self.persona_tone,
                    user=getattr(message.author, "mention", "@user"),
                    channel=mention or "",
                    reason=self.persona_reason,
                )
                persona_ok = True
        except Exception as e:
            log.debug("[lpg-thread-bridge] persona load/guard failed: %r", e)

        if not text:
            base = f"{getattr(message.author, 'mention', '@user')}, *Lucky Pull* terdeteksi."
            text = base + (f" Ke {mention} ya." if mention else "")

        try:
            await message.channel.send(
                text, delete_after=self.persona_delete_after if persona_ok else 10
            )
            if mention:
                log.info("[lpg-thread-bridge] redirect -> %s", mention)
            log.info(
                "[lpg-thread-bridge] persona notice sent (gate=%s)",
                "ok" if persona_ok else "skip",
            )
        except Exception as e:
            log.debug("[lpg-thread-bridge] notice send failed: %r", e)

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if not self.enabled:
            return
        if not message or (
            getattr(message, "author", None) and message.author.bot
        ):
            return
        ch = getattr(message, "channel", None)
        if not ch:
            return
        cid = _cid(ch)
        pid = _pid(ch)
        if not self._in_guard(ch):
            return
        # Must have image
        if not any(self._is_image(a) for a in (message.attachments or [])):
            return

        # Prepare raw bytes & pHash for logging/caching (local only, robust for discord.py slots)
        raw_bytes = None
        ph_val: Optional[int] = None
        try:
            for a in (message.attachments or []):
                ct = (getattr(a, "content_type", None) or "").lower()
                fn = (getattr(a, "filename", "") or "").lower()
                if (ct.startswith("image/") if ct else False) or fn.endswith(
                    (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
                ):
                    raw_bytes = await a.read()
                    break
        except Exception as e:
            log.debug("[lpg-thread-bridge] failed to read bytes for pHash: %r", e)
            raw_bytes = None

        if isinstance(raw_bytes, (bytes, bytearray)):
            try:
                ph_val = _phash64_bytes(raw_bytes)
            except Exception as e:
                log.warning(
                    "[lpg-thread-bridge] _phash64_bytes failed in on_message: %r",
                    e,
                )
                ph_val = None

        # Best-effort stash into message for any consumers that support dynamic attrs
        d = getattr(message, "__dict__", None)
        if isinstance(d, dict):
            d["_nixe_imgbytes"] = raw_bytes
            d["_nixe_phash"] = ph_val

        # Require classification by default
        lucky: bool = False
        score: float = 0.0
        provider: str = "none"
        reason: str = "skip(require_classify=0)"
        provider_hint: Optional[str] = None

        if self.require_classify:
            lucky, score, provider, reason = await self._classify(message)
            log.info(
                "[lpg-thread-bridge] classify: lucky=%s score=%.3f via=%s reason=%s",
                lucky,
                score,
                provider,
                reason,
            )
            provider_hint = (provider or "").lower()

        # Post classification result to status thread (always)
        try:
            # Compute pHash for logging/status embed.
            # We prefer the local ph_val computed earlier in on_message to avoid
            # relying on discord.py dynamic attributes (most models use __slots__).
            ph = ph_val
            if not isinstance(ph, int) and isinstance(raw_bytes, (bytes, bytearray)):
                try:
                    ph = _phash64_bytes(raw_bytes)
                except Exception as e:
                    log.warning(
                        "[lpg-thread-bridge] _phash64_bytes failed in status embed: %r",
                        e,
                    )
                    ph = None

            ph_str = f"{int(ph):016X}" if isinstance(ph, int) else "-"
            fields = [
                ("Result", "✅ LUCKY" if lucky else "❌ NOT LUCKY", True),
                ("Score", f"{float(score or 0.0):.3f}", True),
                ("Provider", provider or "-", True),
                ("Reason", reason or "-", False),
                ("Message ID", str(message.id), True),
                ("Channel", f"<#{getattr(message.channel, 'id', 0)}>", True),
                ("pHash", ph_str, True),
            ]
            await _post_status_embed(
                self.bot,
                title="Lucky Pull Classification",
                fields=fields,
                color=(0x22C55E if lucky else 0xEF4444),
            )
        except Exception:
            pass

        # Assume-lucky on fallback timeouts if explicitly enabled
        if not lucky:
            try:
                assume = os.getenv("LPG_ASSUME_LUCKY_ON_FALLBACK", "0") == "1"
                if assume and isinstance(reason, str) and (
                    "lastchance(" in reason or "shield_fallback(" in reason
                ):
                    log.warning(
                        "[lpg-thread-bridge] assume_lucky_fallback active → forcing redirect (reason=%s)",
                        reason,
                    )
                    lucky = True
                else:
                    return
            except Exception:
                return

        log.info(
            "[lpg-thread-bridge] STRICT_ON_GUARD delete | ch=%s parent=%s type=%s",
            cid,
            pid,
            type(ch).__name__ if ch else None,
        )
        await self._delete_redirect_persona(
            message, lucky, score, provider, reason, provider_hint=provider_hint
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(LPGThreadBridgeGuard(bot))
