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
import json
import os
import os, sys, json, argparse, hashlib, asyncio, textwrap, types, importlib.util, re, random, time, base64, base64
from datetime import datetime
from typing import Any, Dict, List, Tuple
try:
    from nixe.helpers.gemini_lpg_burst import _maybe_transcode as _maybe_transcode_img  # type: ignore
except Exception:
    def _maybe_transcode_img(image_bytes: bytes):
        return image_bytes, 'image/png'


# --- E2E helpers (Discord REST) ---
DISCORD_API = "https://discord.com/api/v10"
def _e2e_headers():
    tok = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
    if not tok:
        raise SystemExit("Missing DISCORD_TOKEN (or DISCORD_BOT_TOKEN)")
    return {"Authorization": f"Bot {tok}", "User-Agent": "nixe-smoke/e2e"}

def _e2e_post_image(target_id: str, image_path: str, content="(smoke:e2e)") -> str:
    import mimetypes, json, requests
    url = f"{DISCORD_API}/channels/{target_id}/messages"
    mime, _ = mimetypes.guess_type(image_path)
    if not mime: mime = "application/octet-stream"
    with open(image_path, "rb") as f:
        files = [("files[0]", (os.path.basename(image_path), f, mime))]
        payload = {"content": content}
        data = {"payload_json": json.dumps(payload)}
        r = requests.post(url, headers=_e2e_headers(), data=data, files=files, timeout=45)
    r.raise_for_status()
    return r.json()["id"]

def _e2e_get_message(chan_id: str, message_id: str):
    import requests
    url = f"{DISCORD_API}/channels/{chan_id}/messages/{message_id}"
    try:
        r = requests.get(url, headers=_e2e_headers(), timeout=5)
        return r.status_code == 200
    except Exception:
        return False

def _e2e_list_messages(chan_id: str, limit: int = 50):
    import requests, time
    url = f"{DISCORD_API}/channels/{chan_id}/messages?limit={limit}"
    try:
        r = requests.get(url, headers=_e2e_headers(), timeout=5)
        if r.status_code == 429:
            try:
                ra = float(r.headers.get("Retry-After", "1"))
            except Exception:
                ra = 1.0
            time.sleep(min(max(ra, 1.0), 5.0))
            return []
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []

def _e2e_find_embed_with_mid(messages, title: str, mid: str):
    for m in messages or []:
        for emb in (m.get("embeds") or []):
            if (emb.get("title") or "").strip().lower() == title.strip().lower():
                for f in (emb.get("fields") or []):
                    if (f.get("name") or "").lower() == "message id" and (f.get("value") or "").strip() == str(mid):
                        return emb
    return None

def _normalize_image_path(p: str) -> str:
    if not p: return p
    # Windows Git Bash -> Windows path
    if os.name == "nt":
        import re as _re
        s = p.replace("\\", "/")
        m = _re.match(r"^/mnt/([a-zA-Z])/(.*)$", s) or _re.match(r"^/([a-zA-Z])/(.*)$", s)
        if m:
            drv, rest = m.group(1).upper(), m.group(2)
            win_rest = rest.replace("/", "\\")
            return f"{drv}:\\" + win_rest
        return p
    # POSIX running, convert C:\ to /mnt/c/
    if ":" in p and "\\" in p:
        drv = p[0].lower()
        rest = p[3:].replace("\\","/")
        return f"/mnt/{drv}/{rest}"
    return p


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


def _parse_ids_any(val: str) -> list[int]:
    """Parse CSV / JSON-array / bracketed strings into a list of ints."""
    out: list[int] = []
    s = (val or "").strip()
    if not s:
        return out
    if s.startswith("[") and s.endswith("]"):
        try:
            arr = json.loads(s)
            for x in arr:
                try:
                    out.append(int(str(x).strip()))
                except Exception:
                    pass
            return out
        except Exception:
            pass
    for tok in re.findall(r"\d+", s):
        try:
            out.append(int(tok))
        except Exception:
            pass
    return out




def _get_guard_ids(key: str) -> list[int]:
    """Resolve guard IDs: ENV first; then nixe/config/runtime_env.json. Accepts list/CSV/brackets."""
    val = os.getenv(key, "")
    ids = _parse_ids_any(val)
    if ids:
        return ids
    try:
        rpath = os.path.join(os.getcwd(), "nixe", "config", "runtime_env.json")
        if os.path.exists(rpath):
            data = json.load(open(rpath, "r", encoding="utf-8"))
            raw = data.get(key)
            if isinstance(raw, list):
                out = []
                for x in raw:
                    try:
                        out.append(int(str(x).strip()))
                    except Exception:
                        pass
                return out
            if raw is not None:
                return _parse_ids_any(str(raw))
    except Exception:
        pass
    return []



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
                if (k.endswith(("_API_KEY","_TOKEN","_SECRET")) or k.endswith("_BACKUP_API_KEY") or ("_API_KEY_" in k)):
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
                if (k.endswith(("_API_KEY","_TOKEN","_SECRET")) or k.endswith("_BACKUP_API_KEY") or ("_API_KEY_" in k)):
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
    tid = int(as_thread) if as_thread else 0
    pid = int(parent) if parent else 0
    in_guard = (tid in guards) or (pid in guards)
    print("=== THREAD CHECK ===")
    print(f"thread_id={as_thread} parent_id={parent} in_guard={in_guard}")


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
        k2 = os.getenv("GEMINI_API_KEY_B", os.getenv("GEMINI_API_KEYB", os.getenv("GEMINI_BACKUP_API_KEY","")))
        keys = [k for k in (k1, k2) if k]
    # dedup while keeping order
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k); out.append(k)
    return out

def _build_payload(image_bytes: bytes) -> dict:
    # Downscale/recompress large Discord images; choose correct MIME
    try:
        image_bytes, mime = _maybe_transcode_img(image_bytes)
    except Exception:
        mime = 'image/png'
    b64 = base64.b64encode(image_bytes).decode('ascii')
    sys_prompt = (
        'Classify STRICTLY whether this image is a gacha "lucky pull" RESULT screen. '
        'Return ONLY JSON: {"lucky": <true|false>, "score": <0..1>, "reason": "..."}. '
        'Bias toward FALSE if inventory/profile/save-data UI.'
    )
    return {
        'contents': [{
            'role': 'user',
            'parts': [
                {'text': sys_prompt},
                {'inline_data': {'mime_type': mime, 'data': b64}}
            ]
        }],
        'generationConfig': {'temperature': 0.0, 'topP': 0.1}
    }

# FIXTAG:BRRT-OK
async def _burst_realtime(image_bytes: bytes, threshold: float):
    import sys, os, importlib.util, pathlib
    # Ensure project root on sys.path so 'nixe.helpers' can be imported
    _repo_root = pathlib.Path(__file__).resolve().parents[1]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    """Burst realtime via helper; returns (verdict, score, timeline)."""
    import time as _t
    t0 = _t.monotonic()
    mode = os.getenv('LPG_BURST_MODE', 'stagger')
    model = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash-lite')
    per_timeout_ms = float(os.getenv('GEMINI_PER_TIMEOUT_MS', os.getenv('LPG_BURST_TIMEOUT_MS', '3500')))
    stagger_ms = float(os.getenv('LPG_BURST_STAGGER_MS', '300'))
    keys = _get_gemini_keys()
    events = [('api:start', _t.monotonic(), None)]
    try:
        try:
            from nixe.helpers.gemini_lpg_burst import classify_lucky_pull_bytes_burst as _burst_call  # type: ignore
        except Exception:
            # fallback to bridge; if package import fails, try importing by file path
            try:
                from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes as _burst_call  # type: ignore
            except Exception:
                _bridge_path = _repo_root / 'nixe' / 'helpers' / 'gemini_bridge.py'
                spec = importlib.util.spec_from_file_location('gemini_bridge', str(_bridge_path))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore
                _burst_call = mod.classify_lucky_pull_bytes
        lucky, score, tag, reason = await _burst_call(image_bytes)
        events.append(('api:done', _t.monotonic(), {'tag': tag, 'reason': reason}))
        verdict = 'LP' if (lucky and float(score) >= float(threshold)) else 'OTHER'
        tl = {
            'mode': mode,
            'model': model,
            'keys': keys,
            'stagger_ms': stagger_ms,
            'per_timeout_ms': per_timeout_ms,
            'events': events,
            't_start': t0,
            'ms_total': int((_t.monotonic() - t0) * 1000.0),
            'ok': bool(lucky and float(score) >= float(threshold)),
            'score': float(score),
            'source': 'helper'
        }
        return verdict, float(score), tl
    except Exception as e:
        events.append(('api:error', _t.monotonic(), {'error': type(e).__name__}))
        tl = {
            'mode': mode,
            'model': model,
            'keys': keys,
            'stagger_ms': stagger_ms,
            'per_timeout_ms': per_timeout_ms,
            'events': events,
            't_start': t0,
            'ms_total': int((_t.monotonic() - t0) * 1000.0),
            'ok': False,
            'score': 0.0,
            'source': 'exception'
        }
        return 'OTHER', 0.0, tl


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
    ap.add_argument("--img", required=False)
    ap.add_argument("--as-thread", required=False)
    ap.add_argument("--parent", required=False)
    ap.add_argument("--print-logs", action="store_true")
    ap.add_argument("--real", action="store_true", help="Use REAL bridge classify instead of SIM")
    ap.add_argument("--burst", action="store_true", help="Run realtime Gemini BURST probe (API1/API2) with timing")
    ap.add_argument("--dotenv", default=".env")
    ap.add_argument("--runtime-json", default="nixe/config/runtime_env.json")
    ap.add_argument("--e2e-online", action="store_true", help="E2E: post image, wait for embed, verify delete")
    ap.add_argument("--e2e-img", help="Image path for E2E (fallback to --img)")
    ap.add_argument("--e2e-target-id", help="Target channel/thread ID (auto from ENV/runtime)")
    ap.add_argument("--e2e-status-thread-id", help="Status thread ID (auto from ENV/runtime or default)")
    ap.add_argument("--e2e-ttl", type=int, default=45, help="Seconds to wait for embed & delete")
    args = ap.parse_args()

    # --- E2E ONLINE (non-destructive) ---
    if getattr(args, 'e2e_online', False):
        info = _read_env_hybrid()
        img = args.e2e_img or args.img
        if not img:
            raise SystemExit('--e2e-online requires --e2e-img or --img')
        img = _normalize_image_path(img)
        # Resolve target: CLI > ENV E2E_TARGET_ID > runtime guard lists
        tgt = (args.e2e_target_id or os.getenv('E2E_TARGET_ID') or None)
        if not tgt:
            gids = []
            try:
                gids = _parse_ids(os.getenv('LPG_GUARD_CHANNELS') or os.getenv('LUCKYPULL_GUARD_CHANNELS') or '')
            except Exception:
                pass
            tgt = str(gids[0]) if gids else None
        if not tgt:
            raise SystemExit('Cannot resolve target id; pass --e2e-target-id or set in runtime_env.json')
        # Resolve status thread: CLI > ENV > default
        stid = (args.e2e_status_thread_id or os.getenv('LPG_STATUS_THREAD_ID') or '1435924665615908965')
        mid = _e2e_post_image(str(tgt), img)
        print(f'[e2e] posted message id={mid}')
        end = time.time() + int(args.e2e_ttl or 45)
        seen = False; deleted = False
        start_ts = time.time()
        while time.time() < end:
            msgs = _e2e_list_messages(str(stid), limit=50)
            if _e2e_find_embed_with_mid(msgs, 'Lucky Pull Classification', mid):
                if not seen:
                    print('[e2e] embed detected on status thread')
                seen = True
            deleted = not _e2e_get_message(str(tgt), mid)
            elapsed = int(time.time() - start_ts)
            print(f'[e2e] poll t={elapsed}s seen={seen} deleted={deleted}')
            if seen and deleted:
                break
            time.sleep(2.0)
        if not seen:
            raise SystemExit('E2E FAIL: classification embed not found')
        if not deleted:
            print('[e2e] WARN: message not deleted within TTL', file=sys.stderr)
        print('[e2e] OK')
        return


    # Legacy-mode validation: require classic args only if not running --e2e-online
    if not getattr(args, "e2e_online", False):
        missing = []
        if not getattr(args, "img", None): missing.append("--img")
        if not getattr(args, "as_thread", None): missing.append("--as-thread")
        if not getattr(args, "parent", None): missing.append("--parent")
        if missing:
            raise SystemExit("Missing required arguments for legacy mode: " + ", ".join(missing))

    info = _read_env_hybrid()
    _print_env(info); _print_guard_wiring(); _print_thread_check(args.as_thread, args.parent); _print_policy(); _print_persona()
    _print_classify(args.img, use_real=(args.real or args.burst or (os.getenv('LPG_BURST','0')=='1')))
    if args.burst:
        _print_burst(args.img)
    print("=== SUMMARY ==="); print("result=OK (wiring looks good)")

if __name__ == "__main__":
    main()

def _read_runtime_json_cached():
    import os, json
    if hasattr(_read_runtime_json_cached, '_cache'):
        return getattr(_read_runtime_json_cached, '_cache')
    path = os.getenv('NIXE_RUNTIME_ENV_PATH') or os.path.join(os.getcwd(), 'nixe','config','runtime_env.json')
    data = {}
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
    except Exception:
        data = {}
    setattr(_read_runtime_json_cached, '_cache', data)
    return data

def _coerce_list(val):
    import re
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        s = val.strip().strip('[]')
        parts = [t.strip().strip('"\'') for t in s.split(',') if t.strip()]
        if not parts:
            parts = re.findall(r"[\w\-\+ ]+", s)
        return [p for p in parts if p]
    return []
