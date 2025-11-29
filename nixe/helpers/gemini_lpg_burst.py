# -*- coding: utf-8 -*-
"""
nixe.helpers.gemini_lpg_burst  (v3: burst + stagger + sequential)
-----------------------------------------------------------------
Dual-Gemini classifier for Lucky Pull with strict time budget and
rate-limit friendly features, plus optional inflight de-duplication
and simple image transcoding.

Returns: (ok: bool, score: float, via: str, reason: str)

Env (supported):
- GEMINI_MODEL              : vision model name (default: "gemini-2.5-flash-lite")
- GEMINI_API_KEYS           : CSV of API keys (preferred)
- GEMINI_API_KEY            : primary key (legacy)
- GEMINI_API_KEY_B / ...    : backup keys (legacy)
- GEMINI_PER_TIMEOUT_MS     : default per-request timeout if LPG_BURST_TIMEOUT_MS unset

- LPG_BURST_MODE            : "stagger" | "parallel" | "sequential" (default: "stagger")
- LPG_BURST_STAGGER_MS      : delay before firing 2nd key in stagger mode (default: 300)
- LPG_BURST_TIMEOUT_MS      : per-provider timeout in ms (default: GEMINI_PER_TIMEOUT_MS or 6000)
- LPG_BURST_EARLY_EXIT_SCORE: score threshold for early exit (default: 0.90)
- LPG_BURST_DEDUP           : "1" to enable inflight de-dup (default: "1")
- LPG_BURST_TAG             : custom tag for "via" result (default: "gemini:{model}")
- LPG_FALLBACK_MARGIN_MS    : margin for sequential mode (soft; default: 1200)

- LPG_NEGATIVE_TEXT         : optional list/CSV of negative phrases that indicate non-result screens
- LPG_FORCE_IPV4            : "1" to prefer IPv4 TCP connector for aiohttp (default: "1")
- LPG_IMG_TRANSCODE         : "1" to enable JPEG transcode/downscale (default: "1")
- LPG_IMG_MAX_SIDE          : max image side when transcoding (default: 1600)
- LPG_IMG_JPEG_QUALITY      : JPEG quality when transcoding (default: 88)
"""

from __future__ import annotations

import os
import atexit
import io
import asyncio
import base64
import json
import logging
import hashlib
import socket
from typing import List, Tuple, Optional

try:
    import aiohttp  # type: ignore
except Exception:  # pragma: no cover
    aiohttp = None

try:
    from PIL import Image  # type: ignore
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover
    Image = None
    np = None


try:
    # Optional safety filter to harden Gemini JSON outputs against deck/collection false positives.
    from nixe.helpers.gemini_result_filters import apply_deck_hardening as _apply_deck_hardening
except Exception:  # pragma: no cover
    _apply_deck_hardening = None

log = logging.getLogger("nixe.helpers.gemini_lpg_burst")


# ---------------------------------------------------------------------------
# env helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    v = os.getenv(key)
    return str(v) if v is not None else default


def _env_f(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except Exception:
        return default


def _keys() -> List[str]:
    """Collect Gemini keys from various envs; order-preserving de-dup."""
    keys: List[str] = []
    raw = _env("GEMINI_API_KEYS", "").strip()
    if raw:
        for part in raw.replace(";", ",").split(","):
            k = part.strip()
            if k and k not in keys:
                keys.append(k)
    for name in [
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_B",
        "GEMINI_API_KEYB",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY2",
        "GEMINI_BACKUP_API_KEY",
    ]:
        v = _env(name, "").strip()
        if v and v not in keys:
            keys.append(v)
    return keys


# ---------------------------------------------------------------------------
# negative phrases / gating
# ---------------------------------------------------------------------------

def _negative_phrases() -> List[str]:
    """
    Resolve negative (non-result) cues for Lucky Pull classification.

    Priority:
      1) LPG_NEGATIVE_TEXT from environment (runtime_env.json).
         Accepts Python literal list, JSON array, or CSV.
      2) Built-in defaults as safety net.

    All values are normalized to lower case and de-duplicated.
    """
    raw = os.getenv("LPG_NEGATIVE_TEXT", "") or ""
    raw = raw.strip()
    words: List[str] = []
    if raw:
        try:
            import ast
            val = ast.literal_eval(raw)
            if isinstance(val, (list, tuple)):
                for x in val:
                    s = str(x).strip().lower()
                    if s:
                        words.append(s)
            else:
                for part in str(val).split(","):
                    s = part.strip().lower()
                    if s:
                        words.append(s)
        except Exception:
            for part in raw.split(","):
                s = part.strip().lower()
                if s:
                    words.append(s)

    defaults = [
        # Promotional / event banners (NOT pull results)
        "activity banner", "event banner", "limited-time event", "event rewards", "version update", "patch notes", "announcement", "login bonus", "free claim", "free to claim", "free outfit", "free skin", "costume set", "event", "version", "banner", "promo", "福利", "活动", "公告", "免费领", "免费领取", "时装", "套装", "全新服装", "皮肤", "礼包",
        # Reward / selector / shop UIs (NOT pull results)
        "rescue merit", "available rewards", "guaranteed rescue", "only once",
        "obtain", "not owned", "reward list", "reward select", "claim reward",
        "exchange", "shop", "store", "purchase", "selector", "currency", "merit",


        # Save/load & meta UI
        "save data", "save_date", "card count", "save slot", "save record",
        "obtained equipment",
        # Loadout / deck / inventory / presets
        "loadout", "edit loadout", "loadout edit",
        "deck", "card deck", "skill deck", "main discs", "disc skills",
        "inventory", "equipment", "equipment list", "gear",
        "artifact", "relic", "emblem", "emblem info", "reforge",
        "build", "build guide", "preset", "ui preset",
        # Navigation / stage / quest UI
        "stage select", "quest", "mission",
        # Character / status / profile / detail
        "profile", "stats", "status screen", "stat page",
        "card skills", "details", "detail",
        # Special game systems
        "potentials", "potential", "memory fragments", "manifest ego",
        "epiphany",
        # Generic popup / info panels
        "select to close", "equip", "equipment info",
        # Web / planner / sheet style (external build charts)
        "spreadsheet", "sheet", "planner", "build table",
    ]
    for d in defaults:
        words.append(d)

    uniq: List[str] = []
    seen = set()
    for w in words:
        w = str(w).strip().lower()
        if not w or w in seen:
            continue
        seen.add(w)
        uniq.append(w)
    return uniq


# ---------------------------------------------------------------------------
# image helpers
# ---------------------------------------------------------------------------


def _normalize_raw_result(res):
    """
    Normalize raw classifier result into a 5-tuple:
    (lucky: bool, score: float, status: str, reason: str, flags: list[str]).

    Accepted shapes:
      - (lucky, score, status, reason, flags)
      - (lucky, score, status, reason)
      - shorter tuples/lists, where missing fields are filled with defaults
      - dicts with keys like "lucky", "score", "status"/"st", "reason", "flags"
      - any other type will be wrapped into a generic error with flags=[repr(res)]
    """
    lucky = False
    score: float = 0.0
    st = "error"
    reason = "bad_result"
    flags: list = []

    if isinstance(res, (tuple, list)):
        seq = list(res)
        if len(seq) >= 1:
            lucky = bool(seq[0])
        if len(seq) >= 2:
            try:
                score = float(seq[1])
            except Exception:
                score = 0.0
        if len(seq) >= 3:
            st = str(seq[2])
        if len(seq) >= 4:
            reason = str(seq[3])
        if len(seq) >= 5:
            flags = list(seq[4:])
    elif isinstance(res, dict):
        lucky = bool(res.get("lucky", False))
        try:
            score = float(res.get("score", 0.0))
        except Exception:
            score = 0.0
        st = str(res.get("status", res.get("st", "ok")))
        reason = str(res.get("reason", ""))
        raw_flags = res.get("flags", [])
        if isinstance(raw_flags, (list, tuple)):
            flags = list(raw_flags)
        elif raw_flags:
            flags = [str(raw_flags)]
    else:
        # unknown type; keep defaults and expose repr in flags
        flags = [repr(res)]

    if not isinstance(flags, list):
        flags = [repr(flags)]
    try:
        score = float(score)
    except Exception:
        score = 0.0
    return bool(lucky), score, str(st or "ok"), str(reason or ""), flags

def _detect_mime(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG"):
        return "image/png"
    return "image/png"


def _phash64(image_bytes: bytes) -> Optional[int]:
    """Simple pHash for inflight de-dup; returns 64-bit int or None."""
    if Image is None or np is None:
        return None
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("L").resize((32, 32))
        arr = np.asarray(im, dtype="float32")
        d = np.fft.fft2(arr).real[:8, :8]
        med = float(np.median(d[1:, 1:]))
        bits = (d[1:, 1:] > med).astype("uint8").flatten()
        val = 0
        for b in bits:
            val = (val << 1) | int(b)
        return int(val)
    except Exception:
        return None


def _maybe_transcode(image_bytes: bytes) -> Tuple[bytes, str]:
    """
    Optionally transcode / downscale large PNGs to JPEG for latency/rate-limit
    friendliness. Controlled by LPG_IMG_TRANSCODE (default: on).
    """
    if _env("LPG_IMG_TRANSCODE", "1") != "1":
        return image_bytes, _detect_mime(image_bytes)
    if Image is None:
        return image_bytes, _detect_mime(image_bytes)
    try:
        im = Image.open(io.BytesIO(image_bytes))
        max_side = int(_env("LPG_IMG_MAX_SIDE", "1600") or "1600")
        if max_side <= 0:
            max_side = 1600
        im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im = im.convert("RGB")
        q = int(_env("LPG_IMG_JPEG_QUALITY", "88") or "88")
        im.save(out, format="JPEG", quality=q, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, _detect_mime(image_bytes)


# ---------------------------------------------------------------------------
# payload / prompt
# ---------------------------------------------------------------------------

def _build_payload(image_bytes: bytes) -> dict:
    # Build Gemini prompt + payload for Lucky Pull detection.
    # Prompt supports both multi-pull (10-pull style) and single-pull result screens.
    image_bytes, mime = _maybe_transcode(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    neg_words = _negative_phrases()
    if neg_words:
        # keep prompt readable; use a compact subset
        max_words = 18
        sample = neg_words[:max_words]
        neg_txt = ", ".join(sample)
    else:
        neg_txt = "save data, card deck, inventory, loadout, profile, status screen"

    sys_prompt = (
        "You are a game UI analyst.\n"
        "Classify STRICTLY whether this screenshot is a gacha PULL RESULT screen "
        "(either a single result or a multi-result 10-pull style screen).\n\n"
        "You must output a single compact JSON object with these keys: "
        "{"
        "\"lucky\": <bool>, "
        "\"score\": <0..1>, "
        "\"reason\": <short string>, "
        "\"flags\": <string[]>, "
        "\"screen_type\": <string>, "
        "\"slot_count\": <int>, "
        "\"is_multi_result_screen\": <bool>"
        "}.\n\n"
        "Definitions:\n"
        "- result_multi_pull: a gacha result screen showing many results at once "
        "(typically 8-12, often 10 or 11). Cards or characters are laid out as vertical "
        "banners or a grid, usually with rarity colors, NEW tags, etc.\n"
        "- result_single_pull: a result screen showing only one gacha result, usually with "
        "a large character or card illustration and rarity stars.\n"
        "- save_data, loadout, deck, inventory, potentials, disc skills, card_detail, "
        "upgrade, epiphany, emblem info, build sheets, or web planners are NOT result screens.\n"
        "Any screen showing only 1-4 cards in the middle (such as Epiphany/card choice/upgrade) "
        "is usually NOT a pull result unless the UI clearly matches a gacha result screen.\n"
        "Any screen showing Save data, Card Count, Manifest Ego, Memory Fragments, equipment or "
        "artifact grids, character details, stat pages, emblem info, or reforging UI is NOT a pull result.\n"
        "Any external website, spreadsheet, or build table (for example skill charts or planners) "
        "is NOT a pull result.\n\nPromotional/event/announcement banners are NOT pull results. They usually contain big headline text and a collage of character arts, often mentioning events/versions/free rewards (e.g., 'event', 'version update', '福利', '活动', '公告', '免费领', '时装', '套装'). If you see these, set lucky=false.\n\n"
        "Rules:\n"
        "- First decide if the screenshot is a gacha pull result at all. If it is clearly a "
        "loadout / deck / save_data / epiphany / build_sheet / planner, set lucky=false.\n"
        "- If the UI looks like a reward/claim/selector/shop screen with ownership/claim labels (e.g., 'Available rewards', 'Only once', 'Guaranteed', 'Obtain', 'Not owned', 'Exchange', 'Shop', 'Merit/Currency'), it is NOT a pull result: set lucky=false.\n"
        "- If the image clearly shows a multi-result gacha result (>=8 result slots at once) you may set "
        "lucky=true, and you MUST set screen_type='result_multi_pull', "
        "is_multi_result_screen=true, and slot_count>=8.\n"
        "- If the image clearly shows a single gacha result screen (one character or card with "
        "rarity stars and result UI), you may set lucky=true, and you MUST set "
        "screen_type='result_single_pull', is_multi_result_screen=false, and slot_count=1.\n"
        "- If the screen shows 14 or more cards or items at once in a dense grid (like a deck or collection view), treat it as NOT a result screen: set lucky=false, use a screen_type containing 'deck' or 'collection', and set is_multi_result_screen=false even if it visually resembles a gacha UI.\n"
        "- If you are unsure, treat it as NOT a result screen (lucky=false).\n"
        f"- Negative UI phrases that strongly indicate NOT a result screen: {neg_txt}.\n"
        "Return ONLY the JSON object. No prose, no markdown."
    )

    return {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": sys_prompt},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.0, "topP": 0.1},
    }


def _parse_json(text: str):
    try:
        j = json.loads(text)
    except Exception:
        import re
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return False, 0.0, "unparseable", []
        try:
            j = json.loads(m.group(0))
        except Exception:
            return False, 0.0, "bad_json", []
    # Harden payload against deck/collection false positives if helper is available.
    if _apply_deck_hardening is not None:
        try:
            j = _apply_deck_hardening(j)
        except Exception:
            # Safety: never let filter failures break classification.
            pass
    lucky = bool(j.get("lucky", False))
    try:
        score = float(j.get("score", 0.0))
    except Exception:
        score = 0.0
    reason = str(j.get("reason", ""))[:240]
    flags = j.get("flags", [])
    if not isinstance(flags, list):
        flags = []
    flags = [str(x).lower() for x in flags]

    # Optional richer schema from the model to allow stricter gating.
    screen_type = str(j.get("screen_type", "") or "").strip().lower()
    slot_count_raw = j.get("slot_count", None)
    try:
        slot_count = int(slot_count_raw)
    except Exception:
        slot_count = None
    multi_raw = j.get("is_multi_result_screen", None)
    is_multi = None
    if isinstance(multi_raw, bool):
        is_multi = multi_raw
    elif isinstance(multi_raw, (int, float)):
        is_multi = bool(multi_raw)
    elif isinstance(multi_raw, str):
        v = multi_raw.strip().lower()
        if v in ("true", "yes", "y", "1"):
            is_multi = True
        elif v in ("false", "no", "n", "0"):
            is_multi = False

    # Heuristic gating: if the model says the UI is clearly non-result, or that
    # there are only a few cards, force lucky=False regardless of the raw flag.
    non_result_keys = (
        "save", "save_data", "loadout", "deck", "inventory", "profile",
        "status", "stat", "detail", "card_detail", "epiphany", "upgrade",
        "manifest", "memory", "equipment", "artifact", "relic",
        "potential", "potentials", "disc", "disc_skill",
        "emblem", "reforge", "build", "sheet", "planner",
        "guide", "record",
    )
    allowed_multi = (
        "result_multi_pull",
        "multi_result",
        "gacha_result_multi",
    )
    allowed_single = (
        "result_single_pull",
        "single_result",
        "gacha_result_single",
        "result_character_single",
    )

    # Hard negatives: jika screen_type jelas non-result, paksa tidak lucky.
    if screen_type:
        st = screen_type
        if any(k in st for k in non_result_keys):
            lucky = False
            score = min(score, 0.5)

    # Structural gating:
    # - Multi result: butuh slot cukup banyak dan tag yang benar.
    # - Single result: boleh jika ditandai jelas sebagai result_single_pull, dsb.
    if lucky:
        if screen_type in allowed_multi or (is_multi is True):
            # Multi → wajib minimal 7 slot dan label yang benar
            if slot_count is not None and slot_count < 7:
                lucky = False
                score = min(score, 0.5)
            if screen_type and screen_type not in allowed_multi:
                lucky = False
                score = min(score, 0.5)
        elif screen_type in allowed_single:
            # Single result → terima selama sudah ditandai jelas dan skornya tidak terlalu kecil.
            if score < 0.6:
                score = 0.6
        else:
            # Model bilang lucky tapi type tidak jelas → kalau slot sedikit, main aman (anggap bukan result).
            if slot_count is not None and slot_count <= 4:
                lucky = False
                score = min(score, 0.5)

    return lucky, max(0.0, min(1.0, score)), reason, flags


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

_SESSION: Optional["aiohttp.ClientSession"] = None  # type: ignore


async def _get_session():
    global _SESSION
    if _SESSION is not None and not getattr(_SESSION, "closed", False):
        return _SESSION
    if aiohttp is None:
        raise RuntimeError("aiohttp_not_available")
    force_ipv4 = (_env("LPG_FORCE_IPV4", "1") == "1")
    connector = None
    if force_ipv4:
        try:
            connector = aiohttp.TCPConnector(family=socket.AF_INET)  # type: ignore[arg-type]
        except Exception:
            connector = None
    _SESSION = aiohttp.ClientSession(connector=connector)
    return _SESSION


@atexit.register
def _close_session_sync():
    global _SESSION
    try:
        s = _SESSION
        _SESSION = None
        if s is not None and not getattr(s, "closed", False):
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(s.close())
                else:
                    loop.run_until_complete(s.close())
            except Exception:
                pass
    except Exception:
        pass


async def _call_one(session, model: str, key: str, image_bytes: bytes, per_timeout: float):
    """
    Low-level call; returns (lucky, score, status, reason, flags).
    status in {"ok","soft_timeout","error","http_error","none:no_result"}.
    """
    if aiohttp is None:
        return False, 0.0, "no_http", "aiohttp_missing", []
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"Content-Type": "application/json; charset=utf-8"}
    payload = _build_payload(image_bytes)
    params = {"key": key}
    try:
        async with session.post(url, headers=headers, params=params, json=payload, timeout=per_timeout) as resp:
            text = await resp.text()
            if resp.status != 200:
                return False, 0.0, "http_error", f"status={resp.status}", [text[:200]]
            try:
                data = json.loads(text)
            except Exception:
                data = {}
            out_text = ""
            try:
                cands = data.get("candidates") or []
                if cands:
                    parts = cands[0].get("content", {}).get("parts", [])
                    if parts:
                        out_text = parts[0].get("text", "") or ""
            except Exception:
                out_text = ""
            if not out_text:
                return False, 0.0, "none:no_result", ["empty_text", text[:200]]
            lucky, score, reason, flags = _parse_json(out_text)
            return lucky, score, "ok", reason, flags
    except asyncio.TimeoutError:
        return False, 0.0, "soft_timeout", "request_timeout", []
    except Exception as e:
        return False, 0.0, "error", repr(e), []


# ---------------------------------------------------------------------------
# burst / stagger / sequential helpers
# ---------------------------------------------------------------------------

async def _do_parallel(session, model, keys, image_bytes, per_timeout, early_score, tag):
    tasks = [asyncio.create_task(_call_one(session, model, k, image_bytes, per_timeout)) for k in keys]
    neg_words = _negative_phrases()
    best = None
    try:
        for coro in asyncio.as_completed(tasks, timeout=per_timeout + 0.1):
            lucky, score, st, reason, flags = _normalize_raw_result(await coro)
            if st == "ok" and any(w in " ".join(flags + [reason]).lower() for w in neg_words):
                lucky = False
                score = min(score, 0.50)
                reason = f"{reason}|neg_cue"
            if lucky and score >= early_score:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                return True, score, tag, f"early({st})"
            if best is None or score > best[1]:
                best = (lucky, score, st, reason, flags)
    except asyncio.TimeoutError:
        pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    if best is None:
        best = (False, 0.0, "none", "no_result", [])
    lucky, score, st, reason, _ = best
    return lucky, score, tag, f"{st}:{reason}"


async def _do_stagger(session, model, keys, image_bytes, per_timeout, early_score, tag, stagger_ms: float):
    # fire key[0], wait a bit, then key[1] if still uncertain/slow
    neg_words = _negative_phrases()
    best = None

    async def _evaluate(res):
        nonlocal best
        lucky, score, st, reason, flags = _normalize_raw_result(res)
        if st == "ok" and any(w in " ".join(flags + [reason]).lower() for w in neg_words):
            lucky = False
            score = min(score, 0.50)
            reason = f"{reason}|neg_cue"
        if lucky and score >= early_score:
            return True, score, tag, f"early({st})"
        if best is None or score > best[1]:
            best = (lucky, score, st, reason, flags)
        return None

    # launch first
    t1 = asyncio.create_task(_call_one(session, model, keys[0], image_bytes, per_timeout))
    try:
        r1 = await asyncio.wait_for(t1, timeout=min(per_timeout, stagger_ms / 1000.0))
        verdict = await _evaluate(r1)
        if verdict:
            return verdict
    except asyncio.TimeoutError:
        # didn't finish before stagger window; continue to fire t2
        pass

    # launch second after stagger
    if len(keys) > 1:
        t2 = asyncio.create_task(_call_one(session, model, keys[1], image_bytes, per_timeout))
    else:
        t2 = None

    # now await whichever finishes first fully
    pending = [t for t in (t1, t2) if t is not None and not t.done()]
    try:
        if pending:
            done, pending = await asyncio.wait(pending, timeout=per_timeout, return_when=asyncio.FIRST_COMPLETED)
            for d in done:
                verdict = await _evaluate(d.result())
                if verdict:
                    for p in pending:
                        p.cancel()
                    return verdict
            # if not early-exit, wait remaining (bounded)
            if pending:
                done2, _ = await asyncio.wait(pending, timeout=per_timeout)
                for d in done2:
                    verdict = await _evaluate(d.result())
                    if verdict:
                        return verdict
    finally:
        for p in (t1, t2) if t2 else (t1,):
            if p and not p.done():
                p.cancel()

    if best is None:
        best = (False, 0.0, "none", "no_result", [])
    lucky, score, st, reason, _ = best
    return lucky, score, tag, f"{st}:{reason}"


async def _do_sequential(session, model, keys, image_bytes, per_timeout, early_score, tag, margin_ms: int):
    """
    Simple sequential mode: call key[0], then optionally key[1] if:
      - status soft_timeout/error, or
      - HTTP 429, or
      - score too low.
    margin_ms is accepted for compat but only used to slightly reduce timeout
    for the first call.
    """
    neg_words = _negative_phrases()
    best = None

    # shorten t1 slightly so second key still has room if needed
    per_timeout1 = per_timeout
    try:
        m = max(0.2, min(per_timeout * 0.5, margin_ms / 1000.0))
        per_timeout1 = max(0.8, per_timeout - m)
    except Exception:
        pass

    # first key
    lucky, score, st, reason, flags = _normalize_raw_result(await _call_one(session, model, keys[0], image_bytes, per_timeout1))
    if st == "ok" and any(w in " ".join(flags + [reason]).lower() for w in neg_words):
        lucky = False
        score = min(score, 0.50)
        reason = f"{reason}|neg_cue"
    best = (lucky, score, st, reason, flags)
    if lucky and score >= early_score:
        return True, score, tag, f"early({st})"

    need_fallback = (st in ("soft_timeout", "error")) or ("status=429" in reason)
    if len(keys) >= 2 and need_fallback:
        lucky2, score2, st2, reason2, flags2 = _normalize_raw_result(await _call_one(session, model, keys[1], image_bytes, per_timeout))
        if st2 == "ok" and any(w in " ".join(flags2 + [reason2]).lower() for w in neg_words):
            lucky2 = False
            score2 = min(score2, 0.50)
            reason2 = f"{reason2}|neg_cue"
        if (lucky2 and score2 >= score) or not lucky:
            return lucky2, score2, tag, f"fallback({st2})"

    lucky, score, st, reason, _ = best
    return lucky, score, tag, f"{st}:{reason}"


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

_INFLIGHT: dict = {}


async def classify_lucky_pull_bytes_burst(image_bytes: bytes):
    """
    Main entry point used by LPG thread bridge.
    Returns (ok, score, via, reason).
    """
    keys = _keys()
    model = _env("GEMINI_MODEL", "gemini-2.5-flash-lite")
    tag = _env("LPG_BURST_TAG", f"gemini:{model}")
    if not keys:
        return False, 0.0, tag, "no_keys"

    timeout_ms_env = os.getenv("LPG_BURST_TIMEOUT_MS") or os.getenv("GEMINI_PER_TIMEOUT_MS")
    try:
        timeout_ms = float(timeout_ms_env) if timeout_ms_env else 6000.0
    except Exception:
        timeout_ms = 6000.0
    per_timeout = max(0.5, timeout_ms / 1000.0)
    early_score = _env_f("LPG_BURST_EARLY_EXIT_SCORE", 0.90)
    mode = _env("LPG_BURST_MODE", "stagger").strip().lower() or "stagger"
    stagger_ms = _env_f("LPG_BURST_STAGGER_MS", 300.0)
    dedup = (_env("LPG_BURST_DEDUP", "1") == "1")

    # Pre-process image (transcode/downscale) for all keys
    processed, _mime = _maybe_transcode(image_bytes)

    async def _runner():
        session = await _get_session()
        if mode == "parallel" or len(keys) < 2:
            return await _do_parallel(session, model, keys, processed, per_timeout, early_score, tag)
        elif mode == "sequential":
            try:
                margin_ms = int(_env("LPG_FALLBACK_MARGIN_MS", "1200") or "1200")
            except Exception:
                margin_ms = 1200
            return await _do_sequential(session, model, keys, processed, per_timeout, early_score, tag, margin_ms)
        else:
            return await _do_stagger(session, model, keys, processed, per_timeout, early_score, tag, stagger_ms)

    if not dedup:
        ok, score, via, reason = await _runner()
        return ok, score, via, reason

    # inflight de-dup keyed by phash+model+mode+len(keys)
    ph = _phash64(processed)
    if ph is None:
        h = hashlib.sha256(processed).hexdigest()
        ph = int(h[:16], 16)
    inflight_key = (ph, model, mode, len(keys))

    fut = _INFLIGHT.get(inflight_key)
    if fut is not None and not fut.done():
        try:
            return await fut
        except Exception:
            _INFLIGHT.pop(inflight_key, None)

    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _INFLIGHT[inflight_key] = fut
    try:
        res = await _runner()
        if not fut.done():
            fut.set_result(res)
        return res
    except Exception as e:  # pragma: no cover
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        try:
            if fut.done():
                _INFLIGHT.pop(inflight_key, None)
        except Exception:
            pass


async def classify_lucky_pull_bytes(image_bytes: bytes):
    """
    Backwards-compatible alias; some callers still import classify_lucky_pull_bytes.
    """
    return await classify_lucky_pull_bytes_burst(image_bytes)