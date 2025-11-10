# nixe/helpers/gemini_bridge.py (parallel-provider variant)
from __future__ import annotations
import os, json, base64, asyncio, logging, typing as T
import aiohttp

log = logging.getLogger(__name__)
__all__ = ["classify_lucky_pull_bytes"]

# optional burst engine import (existing codebase):
_burst = None
_burst_name = None
try:
    from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes_burst as _burst  # type: ignore
    _burst_name = "classify_lucky_pull_bytes_burst"
except Exception:
    try:
        from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes as _burst  # type: ignore
        _burst_name = "classify_lucky_pull_bytes"
    except Exception:
        _burst = None

# config from env (hybrid loader should have applied runtime_env.json already)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_KEY_A = os.getenv("GEMINI_API_KEY")
GEMINI_KEY_B = os.getenv("GEMINI_API_KEY_B")
FORCE_BURST = os.getenv("LPG_BRIDGE_FORCE_BURST", "1") == "1"
ALLOW_QUICK = os.getenv("LPG_BRIDGE_ALLOW_QUICK_FALLBACK", "0") == "1"
PARALLEL = os.getenv("LPG_PROVIDER_PARALLEL", "1") == "1"
SOFT_TIMEOUT_MS = int(os.getenv("LPG_CLASSIFY_SOFT_TIMEOUT_MS", "6000"))
THR = float(os.getenv("GEMINI_LUCKY_THRESHOLD", "0.85"))

def _norm_prob(x: T.Any) -> float:
    try:
        v = float(x)
        if v < 0.0: return 0.0
        if v > 1.0: return 1.0
        return v
    except Exception:
        return 0.0

async def _call_gemini_key(image_bytes: bytes, key: str) -> T.Tuple[dict|None, str|None]:
    if not key:
        return None, "no_key"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "contents": [{
            "parts": [
                {"text": ("You are a strict detector for gacha 'lucky pull' screenshots. "
                          "Return a single JSON object with fields: lucky (true/false), score (0..1), reason (string). "
                          "Be conservative: only lucky=true when the results screen is clear.")},
                {"inline_data": {"mime_type": "image/png", "data": b64}}
            ]
        }],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 256}
    }
    timeout = aiohttp.ClientTimeout(total=max(1.0, SOFT_TIMEOUT_MS / 1000.0))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload) as resp:
                text = await resp.text()
                if resp.status != 200:
                    return None, f"gemini_http_{resp.status}:{text[:200]}"
                try:
                    data = json.loads(text)
                except Exception:
                    return None, f"gemini_parse:{text[:200]}"
                try:
                    txt = data["candidates"][0]["content"]["parts"][0]["text"]
                except Exception:
                    return None, f"gemini_missing_candidate:{str(data)[:200]}"
                i, j = txt.find("{"), txt.rfind("}")
                body = txt[i:j+1] if (i>=0 and j>i) else "{}"
                try:
                    obj = json.loads(body)
                except Exception:
                    return None, "parse_failed"
                return {"lucky": bool(obj.get("lucky", False)),
                        "score": float(obj.get("score", 0.0)),
                        "reason": str(obj.get("reason", "") or "n/a"),
                        "provider": f"gemini:{GEMINI_MODEL}"}, None
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return None, f"exc:{type(e).__name__}:{str(e)[:200]}"

async def _try_parallel_provider(image_bytes: bytes) -> T.Tuple[dict|None, str]:
    keys = []
    if GEMINI_KEY_A:
        keys.append(("primary", GEMINI_KEY_A))
    if GEMINI_KEY_B:
        keys.append(("backup", GEMINI_KEY_B))
    if not keys:
        return None, "no_keys"

    if not PARALLEL or len(keys) == 1:
        last_err = None
        for tag, key in keys:
            res, err = await _call_gemini_key(image_bytes, key)
            if res:
                res["provider_tag"] = tag
                return res, "provider"
            last_err = err
        return None, last_err or "no_result"

    tasks = {asyncio.create_task(_call_gemini_key(image_bytes, key)): tag for tag, key in keys}
    done, pending = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED, timeout=SOFT_TIMEOUT_MS/1000.0)
    for t in done:
        try:
            res, err = await t
        except Exception as e:
            res, err = None, f"task_exc:{type(e).__name__}"
        if res:
            for p in pending:
                p.cancel()
            res["provider_tag"] = tasks[t]
            return res, "provider"
    last_err = None
    for t in done:
        try:
            _, err = await t
        except Exception as e:
            err = f"task_exc:{type(e).__name__}"
        last_err = err or last_err
    for p in pending:
        p.cancel()
    return None, last_err or "no_result"

async def classify_lucky_pull_bytes(image_bytes: bytes) -> T.Tuple[bool, float, str, str]:
    cfg_force = FORCE_BURST
    if cfg_force and _burst is not None:
        try:
            res = await _burst(image_bytes)
            if isinstance(res, tuple):
                if len(res) >= 5:
                    ok, score, _t, via, *_ = res
                    return bool(ok), float(score), str(via), "burst"
                elif len(res) >= 3:
                    ok, score, via = res[:3]
                    return bool(ok), float(score), str(via), "burst"
            return False, 0.0, "burst", "burst_unexpected"
        except Exception as e:
            return False, 0.0, "none", f"burst_error:{type(e).__name__}"

    provider_res, provider_err = await _try_parallel_provider(image_bytes)
    if provider_res is not None:
        score = _norm_prob(provider_res.get("score", 0.0))
        ok = bool(score >= THR)
        tag = provider_res.get("provider_tag", "primary")
        via = f"gemini:{tag}"
        return ok, score, via, provider_res.get("reason", "n/a")

    if ALLOW_QUICK:
        last_err = None
        for key in (GEMINI_KEY_A, GEMINI_KEY_B):
            if not key: continue
            res, err = await _call_gemini_key(image_bytes, key)
            if res:
                score = _norm_prob(res.get("score", 0.0))
                ok = bool(score >= THR)
                tag = "primary" if key == GEMINI_KEY_A else "backup"
                via = f"gemini:{tag}"
                return ok, score, via, res.get("reason", "n/a")
            last_err = err or last_err
        return False, 0.0, "none", f"provider_fallback_failed:{last_err}"

    if _burst is not None:
        try:
            res = await _burst(image_bytes)
            if isinstance(res, tuple):
                if len(res) >= 5:
                    ok, score, _t, via, *_ = res
                    return bool(ok), float(score), str(via), "burst"
                elif len(res) >= 3:
                    ok, score, via = res[:3]
                    return bool(ok), float(score), str(via), "burst"
            return False, 0.0, "burst", "burst_unexpected_after_provider"
        except Exception as e:
            return False, 0.0, "none", f"burst_error_after_provider:{type(e).__name__}"

    return False, 0.0, "none", provider_err or "no_result"
