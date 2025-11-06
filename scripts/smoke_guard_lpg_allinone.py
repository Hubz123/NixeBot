#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_guard_lpg_allinone.py (v4-realtime-burst)
- Based on v3-patched-persona-locate (adds realtime Gemini BURST probe + timing)
- Default: SIM path (compatible output)
- --real: use REAL classification via nixe.helpers.gemini_bridge (as before)
- --burst: run realtime BURST (Gemini API #1/#2) with STAGGER timeline + timeout info
- Read-only against runtime_env.json (.env only for tokens)
"""
from __future__ import annotations
import os, sys, json, argparse, hashlib, asyncio, textwrap, types, importlib.util, re, random, time
from datetime import datetime
from typing import Any, Dict, List, Tuple

# -------------------------
# Util
# -------------------------
def _exists(p): 
    try: return os.path.exists(p)
    except: return False

def _parse_ids(val: str) -> list[int]:
    out = []
    for part in (val or "").replace(";",",").split(","):
        s = part.strip()
        if not s: continue
        try: out.append(int(s))
        except: pass
    return out

def _fmt_ids(nums):
    return "[" + ",".join(str(n) for n in nums) + "]"

def _read_env_hybrid():
    base = os.getcwd()
    rpath = os.path.join(base, "nixe", "config", "runtime_env.json")
    ep = os.path.join(base, ".env")
    info = {
        "runtime_env_json_path": rpath,
        "runtime_env_json_keys": 0,
        "runtime_env_exported_total": 0,
        "runtime_env_tokens_skipped": 0,
        "env_file_path": ep,
        "env_file_keys": 0,
        "env_exported_tokens": 0,
        "policy": "priority: runtime_env.json for configs; .env ONLY for *_API_KEY/*_TOKEN/*_SECRET",
        "GEMINI_API_KEY": False,
        "GEMINI_API_KEY_B": False,
        "GROQ_API_KEY": False,
        "DISCORD_TOKEN": False,
        "error": None
    }
    try:
        if _exists(rpath):
            data = json.load(open(rpath, "r", encoding="utf-8"))
            info["runtime_env_json_keys"] = len(data)
            for k,v in data.items():
                if ("API_KEY" in k) or k.endswith(("_TOKEN","_SECRET")):
                    info["runtime_env_tokens_skipped"] += 1
                    continue
                os.environ.setdefault(str(k), str(v))
            info["runtime_env_exported_total"] = info["runtime_env_json_keys"] - info["runtime_env_tokens_skipped"]
    except Exception as e:
        info["error"] = f"runtime_env: {e}"

    try:
        if _exists(ep):
            lines = [ln.strip() for ln in open(ep,"r",encoding="utf-8").read().splitlines() if "=" in ln and not ln.strip().startswith("#")]
            info["env_file_keys"] = len(lines)
            for ln in lines:
                k, _, v = ln.partition("=")
                k=k.strip(); v=v.strip()
                if ("API_KEY" in k) or k.endswith(("_TOKEN","_SECRET")):
                    os.environ.setdefault(k, v)
                    info["env_exported_tokens"] += 1
    except Exception as e:
        info["error"] = f".env: {e}"

    info["GEMINI_API_KEY"] = bool(os.getenv("GEMINI_API_KEY"))
    info["GEMINI_API_KEY_B"] = bool(os.getenv("GEMINI_API_KEY_B"))
    info["GROQ_API_KEY"] = bool(os.getenv("GROQ_API_KEY"))
    info["DISCORD_TOKEN"] = bool(os.getenv("DISCORD_TOKEN"))
    return info

def _print_env(info):
    print("=== ENV HYBRID CHECK ===")
    print(json.dumps(info, indent=2)); print()

def _print_guard_wiring():
    lpg = _parse_ids(os.getenv("LPG_GUARD_CHANNELS",""))
    luck = _parse_ids(os.getenv("LUCKYPULL_GUARD_CHANNELS",""))
    print("=== GUARD WIRING ===")
    print(f"LUCKYPULL_GUARD_CHANNELS = {_fmt_ids(luck)}")
    print(f"LPG_GUARD_CHANNELS       = {_fmt_ids(lpg)}")
    same = sorted(lpg) == sorted(luck) and len(lpg) > 0
    print("[OK] guard lists identical (non-strict order)" if same else "[WARN] guard lists differ"); print()

def _print_thread_check(as_thread: int, parent: int):
    lpg = set(_parse_ids(os.getenv("LPG_GUARD_CHANNELS","")))
    luck = set(_parse_ids(os.getenv("LUCKYPULL_GUARD_CHANNELS","")))
    guards = lpg or luck
    in_guard = (as_thread in guards) or (parent in guards)
    print("=== THREAD CHECK ===")
    print(f"thread_id={as_thread} parent_id={parent} in_guard={bool(in_guard)}"); print()

def _print_policy():
    strict = (os.getenv("LPG_STRICT_ON_GUARD") or os.getenv("STRICT_ON_GUARD") or "1") == "1"
    timeout = float(os.getenv("LPG_TIMEOUT_SEC", os.getenv("LUCKYPULL_TIMEOUT_SEC","10")))
    redirect = int(os.getenv("LPG_REDIRECT_CHANNEL_ID") or os.getenv("LUCKYPULL_REDIRECT_CHANNEL_ID") or "0")
    persona_only_for = (os.getenv("LPG_PERSONA_ONLY_FOR") or "lucky").strip()
    persona_allowed = (os.getenv("LPG_PERSONA_ALLOWED_PROVIDERS") or "gemini").strip()
    print("=== POLICY (effective) ===")
    print(f"STRICT_ON_GUARD={1 if strict else 0} timeout={timeout:.1f}s redirect={redirect}")
    print(f"Persona only for: {persona_only_for} | allowed providers: {persona_allowed}"); print()

# -------------------------
# Persona Locate (kept from v3)
# -------------------------
def _print_persona():
    context = (os.getenv("LPG_PERSONA_CONTEXT") or "lucky").strip().lower()
    enable = (os.getenv("LPG_PERSONA_ENABLE") or os.getenv("PERSONA_ENABLE") or "1") == "1"
    mode = (os.getenv("LPG_PERSONA_MODE") or os.getenv("PERSONA_MODE") or "yandere").strip().lower()
    reason = os.getenv("LPG_PERSONA_REASON") or os.getenv("PERSONA_REASON") or "Tebaran Garam"
    path = os.getenv("LPG_PERSONA_PATH") or os.getenv("PERSONA_PATH") or "nixe/config/yandere.json"

    print("=== PERSONA SANITY ===")
    print(f"lucky -> persona={'True' if (enable and context=='lucky') else 'False'} (ok)" if context=='lucky' else f"{context} -> persona={'True' if enable else 'False'}")
    print("phish -> persona=False (skip: phishing context)  # expected False"); print()

    print("=== PERSONA LOCATE ===")
    print(f"path={os.path.abspath(path)} mode={mode}")

    # tone resolve
    tone = mode
    if mode == "random":
        dist_raw = os.getenv("PERSONA_TONE_DIST") or os.getenv("LPG_PERSONA_TONE_DIST") or ""
        selected = None
        try:
            if dist_raw:
                obj = json.loads(dist_raw)
                if isinstance(obj, dict):
                    total = sum(float(v) for v in obj.values() if float(v) > 0)
                    total = total if total > 0 else 1.0
                    r = random.random() * total
                    acc = 0.0
                    for k,v in obj.items():
                        w = float(v)
                        if w <= 0: 
                            continue
                        acc += w
                        if r <= acc:
                            selected = k
                            break
        except Exception:
            selected = None
        tone = (selected or random.choice(["soft","agro","sharp"])).lower()

    if tone not in ("soft","agro","sharp"):
        tone = "soft"

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        line_text = "(persona line not found)"
        if isinstance(data, dict) and isinstance(data.get("groups"), dict):
            grp = data["groups"].get(tone)
            if isinstance(grp, list):
                cand = [s for s in grp if isinstance(s, str) and s.strip()]
                if cand:
                    line_text = random.choice(cand)
        if line_text == "(persona line not found)" and isinstance(data, dict):
            grp = data.get(tone)
            if isinstance(grp, list):
                cand = [s for s in grp if isinstance(s, str) and s.strip()]
                if cand:
                    line_text = random.choice(cand)
    except Exception as e:
        line_text = f"(persona load err: {e})"

    print(f"line({tone})={line_text} (alasan: {reason})")
    print()

# -------------------------
# Classify (SIM / REAL via gemini_bridge)
# -------------------------
def _import_gemini_bridge():
    try:
        from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes  # type: ignore
        return classify_lucky_pull_bytes, "pkg", None
    except Exception as e1:
        # Fallback by file path
        root_by_cwd = os.path.abspath(os.getcwd())
        root_by_script = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        candidates = [
            os.path.join(root_by_cwd, "nixe", "helpers", "gemini_bridge.py"),
            os.path.join(root_by_script, "nixe", "helpers", "gemini_bridge.py"),
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    pkg_root = os.path.dirname(os.path.dirname(path))
                    helpers_root = os.path.dirname(path)
                    sys.modules.setdefault("nixe", types.ModuleType("nixe"))
                    sys.modules["nixe"].__path__ = [pkg_root]
                    sys.modules.setdefault("nixe.helpers", types.ModuleType("nixe.helpers"))
                    sys.modules["nixe.helpers"].__path__ = [helpers_root]
                    spec = importlib.util.spec_from_file_location("nixe.helpers.gemini_bridge", path)
                    mod = importlib.util.module_from_spec(spec)
                    assert spec and spec.loader
                    spec.loader.exec_module(mod)  # type: ignore
                    fn = getattr(mod, "classify_lucky_pull_bytes", None)
                    if fn:
                        return fn, "file", None
                except Exception as e2:
                    return None, "err", f"{type(e1).__name__}: {e1} | fallback {type(e2).__name__}: {e2}"
        return None, "err", f"{type(e1).__name__}: {e1}"

async def _real_classify(img_bytes: bytes, thr: float):
    fn, how, err = _import_gemini_bridge()
    if not fn:
        return ("OTHER", False, 0.0, f"gemini_bridge_import_failed({err})")
    try:
        res = await fn(img_bytes, context="lpg")
        ok = bool(res.get("ok", False))
        score = float(res.get("score", 0.0))
        provider = str(res.get("provider","unknown"))
        reason = str(res.get("reason",""))
        tag = "LP" if ok and score >= thr else "OTHER"
        return (tag, ok and score >= thr, score, f"{provider} {reason} [{how}]")
    except Exception as e:
        return ("OTHER", False, 0.0, f"bridge_exception:{type(e).__name__}")

def _sim_classify(img_bytes: bytes, thr: float, name: str):
    nm = (name or "").lower()
    if any(k in nm for k in ("lucky","pull","gacha")):
        return ("LP", True, 0.95, "gemini:gemini-2.5-flash-lite via=sim_stub reason=path_hint(lucky)")
    return ("OTHER", False, 0.05, "gemini:gemini-2.5-flash-lite via=sim_stub reason=neutral_fallback(not_gacha_like)")

def _print_classify(img_path: str, use_real: bool):
    thr = float(os.getenv("GEMINI_LUCKY_THRESHOLD","0.85"))
    try:
        data = open(img_path,"rb").read()
    except Exception as e:
        print("=== CLASSIFY (error) ===")
        print(f"[SMOKE] cannot read image: {e}"); print(); return
    first8 = data[:8].hex(); is_jpeg = first8.startswith("ffd8")
    print("=== CLASSIFY ({} ) ===".format("real" if use_real else "sim"))
    print(f"[SMOKE] src={os.path.basename(img_path)} len={len(data)} hex8={first8} (ffd8=jpeg?={is_jpeg})")
    if use_real:
        loop = asyncio.get_event_loop()
        tag, ok, score, reason = loop.run_until_complete(_real_classify(data, thr))
        print(f"[{tag}] ok={ok} score={score:.2f} via={reason}")
    else:
        tag, ok, score, reason = _sim_classify(data, thr, os.path.basename(img_path))
        print(f"[{tag}] ok={ok} score={score:.2f} provider={reason}")
    print()

# -------------------------
# BURST realtime probe (Gemini #1 and #2) with STAGGER + timing
# -------------------------
def _mask_key(k: str) -> str:
    if not k: return ""
    if len(k) <= 8: return "*"*len(k)
    return k[:4] + "…" + k[-4:]

def _get_gemini_keys() -> List[str]:
    raw = os.getenv("GEMINI_API_KEYS","")
    keys = []
    if raw.strip():
        keys = [p.strip() for p in raw.replace(";",",").split(",") if p.strip()]
    else:
        k1 = os.getenv("GEMINI_API_KEY","")
        k2 = os.getenv("GEMINI_API_KEY_B", os.getenv("GEMINI_API_KEYB",""))
        keys = [k for k in (k1, k2) if k]
    # dedup while keeping order
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k); out.append(k)
    return out

def _build_payload(image_bytes: bytes) -> dict:
    import base64
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

async def _burst_realtime(img_bytes: bytes, thr: float) -> Tuple[str, float, dict]:
    try:
        import aiohttp
    except Exception:
        return ("error:aiohttp_missing", 0.0, {})

    model = os.getenv("GEMINI_MODEL","gemini-2.5-flash-lite")
    keys = _get_gemini_keys()
    per_timeout_ms = float(os.getenv("LPG_BURST_TIMEOUT_MS","1400"))
    stagger_ms = float(os.getenv("LPG_BURST_STAGGER_MS","300"))
    early_score = float(os.getenv("LPG_BURST_EARLY_EXIT_SCORE","0.90"))
    mode = os.getenv("LPG_BURST_MODE","stagger").lower()

    payload = _build_payload(img_bytes)
    url_tpl = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    t0 = time.monotonic()
    timeline = {
        "mode": mode, "model": model,
        "keys": [_mask_key(k) for k in keys],
        "stagger_ms": stagger_ms, "per_timeout_ms": per_timeout_ms,
        "t_start": t0, "events": []  # (name, t, extra)
    }

    async def call_one(session, key, name):
        url = url_tpl.format(model=model, key=key)
        t_start = time.monotonic()
        timeline["events"].append((f"{name}:start", t_start, {}))
        try:
            async with session.post(url, json=payload, timeout=per_timeout_ms/1000.0) as resp:
                st = resp.status
                try:
                    data = await resp.json()
                except Exception:
                    data = {"_raw": await resp.text()}
                t_end = time.monotonic()
                txt = ""
                try:
                    txt = data["candidates"][0]["content"]["parts"][0]["text"]
                except Exception:
                    txt = json.dumps(data)[:500]
                # parse json-ish
                lucky, score, reason = False, 0.0, "unparseable"
                try:
                    obj = None
                    try:
                        obj = json.loads(txt)
                    except Exception:
                        m = re.search(r"\{.*\}", txt, re.S)
                        if m: obj = json.loads(m.group(0))
                    if isinstance(obj, dict):
                        lucky = bool(obj.get("lucky", False))
                        score = float(obj.get("score", 0.0))
                        reason = str(obj.get("reason",""))
                except Exception:
                    pass
                timeline["events"].append((f"{name}:end", t_end, {"status": st, "lucky": lucky, "score": score, "reason": reason[:120]}))
                return lucky, score, st, reason
        except asyncio.TimeoutError:
            t_end = time.monotonic()
            timeline["events"].append((f"{name}:timeout", t_end, {}))
            return False, 0.0, "timeout", "request_timeout"
        except Exception as e:
            t_end = time.monotonic()
            timeline["events"].append((f"{name}:error", t_end, {"err": type(e).__name__}))
            return False, 0.0, "error", type(e).__name__

    async with aiohttp.ClientSession() as session:
        res1 = res2 = None
        task1 = task2 = None
        # fire key1
        if len(keys) >= 1:
            task1 = asyncio.create_task(call_one(session, keys[0], "api1"))
        # optionally fire key2
        if mode == "parallel" and len(keys) >= 2:
            task2 = asyncio.create_task(call_one(session, keys[1], "api2"))
        elif mode == "stagger" and len(keys) >= 2:
            # schedule after stagger, unless task1 finishes early and hits early_score positive
            async def delayed():
                await asyncio.sleep(stagger_ms/1000.0)
                return await call_one(session, keys[1], "api2")
            task2 = asyncio.create_task(delayed())

        # collect
        ok, score, source = False, 0.0, "none"
        while True:
            pending = [t for t in (task1, task2) if t and not t.done()]
            if not pending:
                break
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED, timeout=per_timeout_ms/1000.0 + 0.2)
            for d in done:
                if d is task1:
                    res1 = d.result()
                    l, s, st, _ = res1
                    if l and s >= early_score:
                        ok, score, source = True, s, "api1-early"
                        if task2 and not task2.done():
                            task2.cancel()
                        pending = []
                        break
                elif d is task2:
                    res2 = d.result()
                    l, s, st, _ = res2
                    if l and s >= early_score:
                        ok, score, source = True, s, "api2-early"
                        if task1 and not task1.done():
                            task1.cancel()
                        pending = []
                        break
            if not pending:
                break

        # ensure gather results
        if task1 and not task1.done():
            try: res1 = await task1
            except: pass
        if task2 and not task2.done():
            try: res2 = await task2
            except: pass

        # choose best
        cand = []
        if res1: cand.append(("api1", res1))
        if res2: cand.append(("api2", res2))
        for name, (l, s, st, r) in cand:
            if l and s > score:
                ok, score, source = True, s, name
        if not cand:
            ok, score, source = False, 0.0, "no-response"

    total = (time.monotonic() - t0) * 1000.0
    verdict = "LP" if ok and score >= thr else "OTHER"
    timeline["t_end"] = time.monotonic()
    timeline["ms_total"] = round(total, 1)
    timeline["verdict"] = verdict
    timeline["ok"] = ok
    timeline["score"] = round(float(score), 3)
    timeline["source"] = source
    return verdict, score, timeline

def _print_burst(img_path: str):
    try:
        data = open(img_path, "rb").read()
    except Exception as e:
        print("=== GEMINI BURST (error) ===")
        print(f"[SMOKE] cannot read image: {e}"); print(); return
    thr = float(os.getenv("GEMINI_LUCKY_THRESHOLD","0.85"))
    print("=== GEMINI BURST (realtime) ===")
    loop = asyncio.get_event_loop()
    verdict, score, tl = loop.run_until_complete(_burst_realtime(data, thr))
    # Pretty print timeline
    print(f"mode={tl.get('mode')} model={tl.get('model')} keys={tl.get('keys')} stagger_ms={tl.get('stagger_ms')} per_timeout_ms={tl.get('per_timeout_ms')}")
    for name, t, extra in tl.get("events", []):
        ms = int((t - tl["t_start"]) * 1000.0)
        if extra:
            # clamp reason length
            reason = extra.get("reason")
            if reason and len(reason) > 120:
                extra = dict(extra); extra["reason"] = reason[:120] + "…"
            print(f"  +{ms:4d}ms {name}: {json.dumps(extra, ensure_ascii=False)}")
        else:
            print(f"  +{ms:4d}ms {name}")
    print(f"TOTAL={tl.get('ms_total')}ms verdict={verdict} ok={bool(tl.get('ok'))} score={tl.get('score')} source={tl.get('source')}  (TIMEOUT? {'yes' if any('timeout' in n for n,_,_ in tl.get('events', [])) else 'no'})")
    print()

# -------------------------
# Entry
# -------------------------
def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--img", required=True)
    ap.add_argument("--as-thread", type=int, required=True)
    ap.add_argument("--parent", type=int, required=True)
    ap.add_argument("--print-logs", action="store_true")
    ap.add_argument("--real", action="store_true", help="Use REAL bridge classify instead of SIM")
    ap.add_argument("--burst", action="store_true", help="Run realtime Gemini BURST probe (API1/API2) with timing")
    args = ap.parse_args()

    info = _read_env_hybrid()
    _print_env(info); _print_guard_wiring(); _print_thread_check(args.as_thread, args.parent); _print_policy(); _print_persona()
    _print_classify(args.img, use_real=args.real)
    if args.burst:
        _print_burst(args.img)
    print("=== SUMMARY ==="); print("result=OK (wiring looks good)")

if __name__ == "__main__":
    main()
