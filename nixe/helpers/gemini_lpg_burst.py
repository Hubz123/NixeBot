
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
    ]:
        if k:
            candidates.append(k)
    seen, out = set(), []
    for k in candidates:
        if k not in seen:
            seen.add(k); out.append(k)
    return out



def _negative_phrases() -> List[str]:
    return [
        "save data", "save-data", "card count", "obtained equipment",
        "manifest ego", "partners", "memory fragments", "potential"
    ]

def _sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()

# inflight dedup for identical bytes (optional)
_INFLIGHT: dict[str, asyncio.Future] = {}


# -------- http session helpers --------
_SESSION = None

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
def _build_payload(image_bytes: bytes) -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    sys_prompt = (
        "Classify STRICTLY whether this image is a gacha 'lucky pull' RESULT screen. "
        "Respond ONLY JSON: {\"lucky\": <bool>, \"score\": <0..1>, \"reason\": <short>, \"flags\": <string[]>}. "
        "Bias toward FALSE if it's inventory/loadout/profile/status. "
        "Positive cues: 10-pull grid, NEW!!, rainbow beam, multiple result slots. "
        "Negative cues: 'Save data', 'Card Count', 'Obtained Equipment', 'Manifest Ego', partners, memory fragments."
    )
    return {
        "contents": [
            {"role": "user", "parts": [
                {"text": sys_prompt},
                {"inline_data": {"mime_type": "image/png", "data": b64}}
            ]}
        ],
        "generationConfig": {"temperature": 0.0, "topP": 0.1}
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
    per_timeout = _env_f("LPG_BURST_TIMEOUT_MS", 1400) / 1000.0
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

