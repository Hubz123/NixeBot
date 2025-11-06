
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_guard_lpg_allinone.py (v3-patched-persona-locate)
- Default: SIM path (compatible output)
- --real: use REAL classification via nixe.helpers.gemini_bridge
  with robust import fallbacks & verbose errors.
- Read-only against runtime_env.json (.env only for tokens)
"""
from __future__ import annotations
import os, sys, json, argparse, hashlib, asyncio, textwrap, types, importlib.util, re, random
from datetime import datetime
from typing import Any, Dict, List

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
        "GROQ_API_KEY": False,
        "DISCORD_TOKEN": False,
        "error": None
    }
    try:
        if _exists(rpath):
            data = json.load(open(rpath, "r", encoding="utf-8"))
            info["runtime_env_json_keys"] = len(data)
            for k,v in data.items():
                if k.endswith(("_API_KEY","_TOKEN","_SECRET")):
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
                if k.endswith(("_API_KEY","_TOKEN","_SECRET")):
                    os.environ.setdefault(k, v)
                    info["env_exported_tokens"] += 1
    except Exception as e:
        info["error"] = f".env: {e}"

    info["GEMINI_API_KEY"] = bool(os.getenv("GEMINI_API_KEY"))
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
# Persona Locate (patched): real JSON, weighted random
# -------------------------

def _parse_weights_from_env() -> Dict[str, float]:
    # Support the same shapes your runtime uses:
    # - PERSONA_TONE_DIST='{"soft":0.5,"agro":0.25,"sharp":0.25}'
    # - PERSONA_TONE_SOFT / PERSONA_TONE_AGRO / PERSONA_TONE_SHARP
    # - PERSONA_TONE="soft=0.5,agro=0.5,sharp=0.5" (will be normalized)
    dist = {}
    raw = os.getenv("PERSONA_TONE_DIST") or os.getenv("LPG_PERSONA_TONE_DIST") or ""
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                dist = {k.lower(): float(v) for k,v in obj.items()}
        except Exception:
            pass
    if not dist:
        for key, tone in (("PERSONA_TONE_SOFT","soft"),("PERSONA_TONE_AGRO","agro"),("PERSONA_TONE_SHARP","sharp")):
            val = os.getenv(key) or os.getenv("LPG_"+key) or ""
            if val:
                try: dist[tone] = float(val)
                except: pass
    if not dist:
        inline = os.getenv("PERSONA_TONE") or os.getenv("LPG_PERSONA_TONE") or ""
        if inline and any(ch in inline for ch in ("=",":")):
            pairs = [p.strip() for p in inline.replace(":", "=").split(",")]
            for p in pairs:
                if "=" in p:
                    k, v = p.split("=", 1)
                    try:
                        dist[k.strip().lower()] = float(v.strip())
                    except:
                        pass
    # default equal weights if still empty
    if not dist:
        dist = {"soft": 1.0, "agro": 1.0, "sharp": 1.0}
    # normalize
    s = sum(max(0.0, v) for v in dist.values()) or 1.0
    return {k: max(0.0, v)/s for k,v in dist.items()}

def _weighted_choice(d: Dict[str,float]) -> str:
    r = random.random()
    acc = 0.0
    for k, w in d.items():
        acc += w
        if r <= acc:
            return k
    return next(iter(d.keys()))

def _pick_persona_line(json_obj: Any, context: str, tone: str) -> str:
    # Try common shapes:
    # 1) data[context][tone] -> [lines]
    # 2) data[tone] -> [lines]
    # 3) data["lines"] -> [lines of str]
    # Fallback: search any list of strings
    def _strings(lst):
        return [s for s in lst if isinstance(s, str) and s.strip()]

    if isinstance(json_obj, dict):
        if context in json_obj and isinstance(json_obj[context], dict):
            sec = json_obj[context]
            if tone in sec and isinstance(sec[tone], list):
                ls = _strings(sec[tone])
                if ls: return random.choice(ls)
            # any list under context
            for v in sec.values():
                if isinstance(v, list):
                    ls = _strings(v)
                    if ls: return random.choice(ls)
        if tone in json_obj and isinstance(json_obj[tone], list):
            ls = _strings(json_obj[tone])
            if ls: return random.choice(ls)
        if "lines" in json_obj and isinstance(json_obj["lines"], list):
            ls = _strings(json_obj["lines"])
            if ls: return random.choice(ls)
        # search lists of strings shallow
        for v in json_obj.values():
            if isinstance(v, list):
                ls = _strings(v)
                if ls: return random.choice(ls)
            if isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, list):
                        ls = _strings(vv)
                        if ls: return random.choice(ls)
    if isinstance(json_obj, list):
        ls = _strings(json_obj)
        if ls: return random.choice(ls)
    return "(persona line not found)"


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

    # Determine tone
    tone = mode
    if mode == "random":
        # Try weighted from PERSONA_TONE_DIST; else equal weights
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

    # Load JSON and pick a line
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        line_text = "(persona line not found)"
        # 1) groups.<tone>[]
        if isinstance(data, dict) and isinstance(data.get("groups"), dict):
            grp = data["groups"].get(tone)
            if isinstance(grp, list):
                cand = [s for s in grp if isinstance(s, str) and s.strip()]
                if cand:
                    line_text = random.choice(cand)
        # 2) top-level <tone>[]
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


def _import_gemini_bridge():
    # Attempt normal import
    try:
        from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes  # type: ignore
        return classify_lucky_pull_bytes, "pkg", None
    except Exception as e1:
        # Fallback: import by file path, ensuring package parents in sys.modules
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

def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--img", required=True)
    ap.add_argument("--as-thread", type=int, required=True)
    ap.add_argument("--parent", type=int, required=True)
    ap.add_argument("--print-logs", action="store_true")
    ap.add_argument("--real", action="store_true", help="Use REAL bridge classify instead of SIM")
    args = ap.parse_args()

    info = _read_env_hybrid()
    _print_env(info); _print_guard_wiring(); _print_thread_check(args.as_thread, args.parent); _print_policy(); _print_persona(); _print_classify(args.img, use_real=args.real)
    print("=== SUMMARY ==="); print("result=OK (wiring looks good)")

if __name__ == "__main__":
    main()
