
# -*- coding: utf-8 -*-
"""
nixe.helpers.gemini_lpg_burst  (v2: stagger + inflight dedup)
----------------------------------------------------------------
Dual-Gemini classifier for Lucky Pull with strict time budget and
rate-limit friendly features.

Returns: (ok: bool, score: float, via: str, reason: str)

New env (optional):
- LPG_BURST_MODE: "stagger" | "parallel"   (default: "stagger")
- LPG_BURST_STAGGER_MS: delay before firing 2nd key (default: 300)\n- LPG_FALLBACK_MARGIN_MS: sequential fallback margin (default: 1200)\n- LPG_BURST_MODE: 'sequential' | 'stagger' | 'parallel' (default: 'sequential' for Render Free)
- LPG_BURST_DEDUP: "1" to de-dup concurrent identical images (default: "1")
Existing env:
- GEMINI_MODEL (default: "gemini-2.5-flash-lite")
- GEMINI_API_KEYS or GEMINI_API_KEY + GEMINI_API_KEY_2
- LPG_BURST_TIMEOUT_MS (per-key, default: 1400)
- LPG_BURST_EARLY_EXIT_SCORE (default: 0.90)
- LPG_BURST_TAG (default: "gemini:burst-2keys")
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
from typing import Tuple, Optional, List

try:
    import aiohttp
except Exception:
    aiohttp = None

log = logging.getLogger(__name__)

# -------- util/env --------
def _env(k: str, d: str = "") -> str:
    v = os.getenv(k)
    return str(v) if v is not None else d

def _env_f(k: str, d: float) -> float:
    try:
        return float(_env(k, str(d)))
    except Exception:
        return d


def _keys() -> List[str]:
    # Merge from CSV + individual envs (order-preserving de-dup)
    raw = _env("GEMINI_API_KEYS", "")
    candidates: List[str] = []
    if raw.strip():
        candidates.extend([p.strip() for p in raw.replace(";", ",").split(",") if p.strip()])
    for k in [
        _env("GEMINI_API_KEY",""),
        _env("GEMINI_API_KEY_B",""),
        _env("GEMINI_API_KEYB",""),
        _env("GEMINI_API_KEY_2",""),
        _env("GEMINI_API_KEY2",""),
        _env("GEMINI_BACKUP_API_KEY",""),
    ]:
        if k:
            candidates.append(k)
    seen, out = set(), []
    for k in candidates:
        if k not in seen:
            seen.add(k); out.append(k)
    return out



def _negative_phrases() -> List[str]:
    """
    Resolve negative (non-result) cues for Lucky Pull classification.

    Priority:
      1) LPG_NEGATIVE_TEXT from environment (populated via runtime_env.json).
         - Accepts Python-list-like, JSON list, or comma-separated string.
      2) Built-in defaults as safety net.

    All values are normalized to lower case and de-duplicated.
    """
    raw = os.getenv("LPG_NEGATIVE_TEXT", "").strip()
    words: List[str] = []
    if raw:
        try:
            import ast as _ast
            val = _ast.literal_eval(raw)
            if isinstance(val, (list, tuple)):
                words = [str(x).strip().lower() for x in val if str(x).strip()]
            else:
                words = [w.strip().lower() for w in str(val).split(",") if w.strip()]
        except Exception:
            words = [w.strip().lower() for w in raw.split(",") if w.strip()]

    defaults = [
        # Save/load & meta UI
        "save data", "save date", "card count", "save slot", "save record",
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
        "emblem info", "select to close", "equip", "equipment info",
        # Web / planner / sheet style (external build charts)
        "spreadsheet", "sheet", "planner", "build table",
    ]
    # append defaults after config to preserve user priority but ensure we always have a floor
    for d in defaults:
        words.append(d)

    seen = set()
    uniq: List[str] = []
    for w in words:
        w = str(w).strip().lower()
        if not w or w in seen:
            continue
        seen.add(w)
        uniq.append(w)
    return uniq

def _sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()

# inflight dedup for identical bytes (optional)
_INFLIGHT: dict[str, asyncio.Future] = {}


# -------- http session helpers --------
_SESSION = None


def _close_session_sync():
    global _SESSION
    try:
        s = _SESSION
        _SESSION = None
        if s is not None and not getattr(s, 'closed', True):
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

atexit.register(_close_session_sync)
def _make_connector():
    try:
        import socket, aiohttp
        force_ipv4 = _env("LPG_FORCE_IPV4","1") == "1"
        args = dict(limit=8, ttl_dns_cache=300)
        if force_ipv4:
            args["family"] = getattr(socket, "AF_INET", 2)
        return aiohttp.TCPConnector(**args)
    except Exception:
        return None

async def _get_session():
    global _SESSION
    if _SESSION is None or getattr(_SESSION, "closed", True):
        _SESSION = aiohttp.ClientSession(connector=_make_connector())
    return _SESSION

async def _warmup(session, key: str):
    if _env("LPG_BURST_WARMUP","1") != "1":
        return
    try:
        import aiohttp
        url = f"https://generativelanguage.googleapis.com/v1/models?key={key}"
        t = aiohttp.ClientTimeout(total=1.0)
        async with session.get(url, timeout=t) as r:
            await r.release()
    except Exception:
        pass


# -------- request build/parse --------

def _maybe_transcode(image_bytes: bytes) -> tuple[bytes, str]:
    """
    Try to downscale/transcode to JPEG to shrink payload.
    Controlled by env:
      - LPG_IMG_TRANSCODE (default '1')
      - LPG_IMG_MAX_DIM (default '1024')  # longest side
      - LPG_IMG_JPEG_QUALITY (default '85')
      - LPG_IMG_MAX_JPEG_KB (default '256')  # soft cap
    Returns: (bytes, mime_type)
    """
    try:
        if _env("LPG_IMG_TRANSCODE","1") != "1":
            return image_bytes, "image/png"
        try:
            from PIL import Image
            import io
        except Exception:
            return image_bytes, "image/png"
        # Load image
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        max_dim = int(_env("LPG_IMG_MAX_DIM", _env("LPG_IMG_MAX_SIDE","1024")))
        w, h = im.size
        if max(w,h) > max_dim and max_dim > 0:
            scale = max_dim / float(max(w,h))
            im = im.resize((max(1,int(w*scale)), max(1,int(h*scale))), Image.LANCZOS)
        # Encode JPEG
        buf = io.BytesIO()
        quality = int(_env("LPG_IMG_JPEG_Q", _env("LPG_IMG_JPEG_QUALITY","85")))
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        cap_kb = int(_env("LPG_IMG_TARGET_KB", _env("LPG_IMG_MAX_JPEG_KB","256")))
        # If still too large, re-encode at lower quality 70 then 60
        if len(data) > cap_kb*1024:
            for q in (70, 60, 50):
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=q, optimize=True)
                data = buf.getvalue()
                if len(data) <= cap_kb*1024:
                    break
        return data, "image/jpeg"
    except Exception:
        return image_bytes, "image/png"

def _build_payload(image_bytes: bytes) -> dict:
    """
    Build Gemini prompt + payload for Lucky Pull detection.

    We ask the model for a richer schema (screen_type, slot_count, etc.)
    so that the client can enforce strict multi-result semantics and avoid
    deleting non-gacha UI like loadouts, Epiphany, save data, or build sheets.
    """
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
        "Classify STRICTLY whether this screenshot is a gacha PULL RESULT screen.\n\n"
        "You must output a single compact JSON object with these keys: "
        "{"
        "\"lucky\": <bool>, "
        "\"score\": <0..1>, "
        "\"reason\": <short string>, "
        "\"flags\": <string[]>, "
        "\"screen_type\": <string>, "
        "\"slot_count\": <int>, "
        "\"is_multi_result_screen\": <bool>"
        "}."
        "\n\n"
        "Definitions:\n"
        "- result_multi_pull: a gacha result screen showing many results at once "
        "(typically 8-12, often 10 or 11). Cards or characters are laid out as vertical "
        "banners or a grid, usually with rarity colors, NEW tags, etc.\n"
        "- single_pull: a result screen showing only one result.\n"
        "- save_data, loadout, deck, inventory, potentials, disc skills, card_detail, "
        "upgrade, epiphany, emblem info, build sheets, or web planners are NOT result screens.\n"
        "Any screen showing only 1-4 cards in the middle (such as Epiphany/card choice/upgrade) "
        "is NOT a pull result.\n"
        "Any screen showing Save data, Card Count, Manifest Ego, Memory Fragments, equipment or "
        "artifact grids, character details, stat pages, emblem info, or reforging UI is NOT a pull result.\n"
        "Any external website, spreadsheet, or build table (for example skill charts or planners) "
        "is NOT a pull result.\n\n"
        "Rules:\n"
        "- If the image is not a multi-result gacha pull result screen (10-pull style or similar), "
        "set lucky=false, is_multi_result_screen=false, slot_count to the estimated number of cards, "
        "and screen_type to an appropriate non-result label (e.g. 'epiphany', 'save_data', 'deck', "
        "'upgrade', 'potentials', 'disc_skills', 'build_sheet').\n"
        "- Only if the image clearly shows a multi-result gacha result (>=8 result slots at once) may you set "
        "lucky=true. In that case you MUST set screen_type='result_multi_pull', "
        "is_multi_result_screen=true, and slot_count>=8.\n"
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
    if screen_type:
        st = screen_type
        if any(k in st for k in non_result_keys):
            lucky = False
            score = min(score, 0.5)

    # Require a reasonably large multi-result screen for lucky=True.
    if slot_count is not None and slot_count < 7:
        lucky = False
        score = min(score, 0.5)
    if is_multi is False:
        lucky = False
        score = min(score, 0.5)
    if lucky:
        if screen_type and screen_type not in (
            "result_multi_pull",
            "multi_result",
            "gacha_result_multi",
        ):
            lucky = False
            score = min(score, 0.5)

    return lucky, max(0.0, min(1.0, score)), reason, flags

async def _call_one(session, model: str, key: str, image_bytes: bytes, timeout_s: float):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = _build_payload(image_bytes)
    try:
        async with session.post(url, json=payload, timeout=timeout_s) as resp:
            if resp.status != 200:
                # ## RETRY_ON_429: quick single retry if we still have headroom
                if resp.status in (429, 503) and timeout_s >= 2.2:
                    try:
                        await asyncio.sleep(0.25)
                        to2 = aiohttp.ClientTimeout(total=timeout_s - 0.8)
                        async with session.post(url, json=payload, timeout=to2) as r2:
                            if r2.status == 200:
                                data = await r2.json()
                                text = ""
                                try:
                                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                                except Exception:
                                    text = json.dumps(data)[:500]
                                lucky, score, reason, flags = _parse_json(text)
                                return lucky, score, "ok", reason, flags
                    except Exception:
                        pass
                return False, 0.0, "http", f"status={resp.status}", []
            data = await resp.json()
            # extract text
            text = ""
            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                text = json.dumps(data)[:500]
            lucky, score, reason, flags = _parse_json(text)
            return lucky, score, "ok", reason, flags
    except asyncio.TimeoutError:
        return False, 0.0, "soft_timeout", "request_timeout", []
    except Exception as e:
        return False, 0.0, "error", f"{type(e).__name__}", []

# -------- core: burst with modes --------
async def _do_parallel(session, model, keys, image_bytes, per_timeout, early_score, tag):
    tasks = [asyncio.create_task(_call_one(session, model, k, image_bytes, per_timeout)) for k in keys]
    neg_words = _negative_phrases()
    best = (False, 0.0, "none", "no_result", [])
    try:
        for coro in asyncio.as_completed(tasks, timeout=per_timeout+0.1):
            lucky, score, st, reason, flags = await coro
            if any(w in " ".join(flags + [reason]).lower() for w in neg_words):
                lucky = False; score = min(score, 0.50); reason = f"{reason}|neg_cue"
            if lucky and score >= early_score:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                return True, score, tag, f"early({st})"
            if score > best[1]:
                best = (lucky, score, st, reason, flags)
    except asyncio.TimeoutError:
        pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    lucky, score, st, reason, _ = best
    return lucky, score, tag, f"{st}:{reason}"

async def _do_stagger(session, model, keys, image_bytes, per_timeout, early_score, tag, stagger):
    # fire key[0], wait a bit, then key[1] if still uncertain/slow
    neg_words = _negative_phrases()
    best = (False, 0.0, "none", "no_result", [])

    async def _evaluate(res):
        nonlocal best
        lucky, score, st, reason, flags = res
        if any(w in " ".join(flags + [reason]).lower() for w in neg_words):
            lucky = False; score = min(score, 0.50); reason = f"{reason}|neg_cue"
        if lucky and score >= early_score:
            return True, score, tag, f"early({st})"
        if score > best[1]:
            best = (lucky, score, st, reason, flags)
        return None

    # launch first
    t1 = asyncio.create_task(_call_one(session, model, keys[0], image_bytes, per_timeout))
    try:
        r1 = await asyncio.wait_for(t1, timeout=min(per_timeout, stagger/1000.0))
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
    pending = [t for t in [t1, t2] if t is not None and not t.done()]
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
        for p in [t1, t2] if t2 else [t1]:
            if p and not p.done():
                p.cancel()

    lucky, score, st, reason, _ = best
    return lucky, score, tag, f"{st}:{reason}"

# -------- public API --------
async def classify_lucky_pull_bytes_burst(image_bytes: bytes):
    tag = _env("LPG_BURST_TAG", "gemini:burst-2keys")
    if aiohttp is None:
        return False, 0.0, tag, "aiohttp_missing"

    keys = _keys()
    if not keys:
        return False, 0.0, tag, "no_keys"

    model = _env("GEMINI_MODEL", "gemini-2.5-flash-lite")
    per_timeout = _env_f("LPG_BURST_TIMEOUT_MS", float(os.getenv("GEMINI_PER_TIMEOUT_MS","6000"))) / 1000.0
    early_score = _env_f("LPG_BURST_EARLY_EXIT_SCORE", 0.90)
    mode = _env("LPG_BURST_MODE", "stagger").lower()
    stagger_ms = _env_f("LPG_BURST_STAGGER_MS", 300)
    dedup = _env("LPG_BURST_DEDUP", "1") == "1"

    # Prepare payload
    tag = f"gemini:{model}"

    async def _parallel_or_stagger(session):
        if mode == "parallel" or len(keys) < 2:
            return await _do_parallel(session, model, keys, image_bytes, per_timeout, early_score, tag)
        else:
            if mode == "sequential":
                return await _do_sequential_deadline(session, model, keys, image_bytes, per_timeout, early_score, tag, int(_env('LPG_FALLBACK_MARGIN_MS','1200')))
            else:
                return await _do_stagger(session, model, keys, image_bytes, per_timeout, early_score, tag, stagger_ms)

    # simple inflight de-dup per image digest + mode
    key = f"{hash(image_bytes)}|{mode}|{per_timeout}|{stagger_ms}"
    if dedup:
        fut = _INFLIGHT.get(key)
        if fut and not fut.done():
            try:
                res = await fut
                return res
            except Exception:
                pass
        import asyncio

        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        _INFLIGHT[key] = fut
        try:
            session = await _get_session()
            await _warmup(session, keys[0] if keys else _env('GEMINI_API_KEY',''))
            res = await _parallel_or_stagger(session)
            if not fut.done():
                fut.set_result(res)
            return res
        finally:
            _INFLIGHT.pop(key, None)
    else:
        session = await _get_session()
        await _warmup(session, keys[0] if keys else _env('GEMINI_API_KEY',''))
        return await _parallel_or_stagger(session)




async def _do_sequential_deadline(session, model, keys, image_bytes, per_timeout, early_score, tag, margin_ms):
    import time
    neg_words = _negative_phrases()
    start_t = time.monotonic()
    k1 = keys[0]
    t1 = max(1.6, per_timeout - min((margin_ms/1000.0), max(0.6, per_timeout*0.20)))
    lucky, score, st, reason, flags = await _call_one(session, model, k1, image_bytes, t1)
    if any(w in " ".join(flags + [reason]).lower() for w in neg_words):
        lucky = False; score = min(score, 0.50); reason = f"{reason}|neg_cue"
    if lucky and score >= early_score:
        return True, score, tag, f"early({st})"
    need_fallback = (st in ("soft_timeout","error")) or (st == "http" and "status=429" in reason) or (time.monotonic() - start_t) >= t1
    if (len(keys) >= 2) and need_fallback:
        k2 = keys[1]
        lucky2, score2, st2, reason2, flags2 = await _call_one(session, model, k2, image_bytes, per_timeout)
        if any(w in " ".join(flags2 + [reason2]).lower() for w in neg_words):
            lucky2 = False; score2 = min(score2, 0.50); reason2 = f"{reason2}|neg_cue"
        if (lucky2 and score2 >= score) or not lucky:
            return lucky2, score2, tag, f"fallback({st2})"
    return lucky, score, st, reason
