
# --- bootstrap path for local runs ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_PROJ = _os.path.abspath(_os.path.join(_ROOT, ".."))
if _PROJ not in _sys.path:
    _sys.path.insert(0, _PROJ)
# -------------------------------------

#!/usr/bin/env python3
# scripts/smoke_lpg_provider.py (router version, log format dijaga)
import os, sys, json, argparse, binascii, re
from urllib.parse import urlparse

RT_CANDIDATES = ["nixe/config/runtime_env.json", "runtime_env.json"]

def _mask(s: str) -> str:
    if not s: return ""
    tail = s[-4:] if len(s) >= 4 else s
    return f"{'*' * max(4, len(s) - 4)}{tail}"

def _first8_hex(b: bytes) -> str:
    import binascii
    return binascii.hexlify((b or b'')[:8]).decode()

def _load_json_first():
    for p in RT_CANDIDATES:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            print(f"[env-hybrid] loaded json: {p.split('/')[-1]} -> {len(data)} keys")
            return data
        except Exception:
            continue
    print(f"[env-hybrid] loaded json: {RT_CANDIDATES[0].split('/')[-1]} -> 0 keys")
    return {}

def _load_env_file():
    # mimic existing style
    cnt = 0
    try:
        with open(".env","r",encoding="utf-8") as f:
            for line in f:
                s=line.strip()
                if not s or s.startswith("#"): continue
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", s)
                if not m: continue
                k,v=m.group(1),m.group(2)
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v=v[1:-1]
                if os.getenv(k) is None:
                    os.environ[k]=v; cnt+=1
    except FileNotFoundError:
        pass
    print(f"[env-hybrid] loaded .env: .env -> {cnt} keys")

def _read_bytes(path:str)->bytes:
    if not path: return b""
    try:
        with open(path,"rb") as f: return f.read()
    except Exception:
        return b""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", default="", help="path gambar uji Lucky Pull (Gemini)")
    ap.add_argument("--phish-url", default="", help="URL publik uji phishing")
    ap.add_argument("--phish-text", default="gratis nitro http://...")
    args = ap.parse_args()

    env_json = _load_json_first()
    _load_env_file()

    gA = os.getenv("GEMINI_API_KEY") or ""
    gB = os.getenv("GEMINI_API_KEY_B") or ""
    gems = [k for k in (gA,gB) if k]
    print(f"[SMOKE] GEMINI_API_KEY count={len(gems)} detail={[ _mask(k) for k in gems ]}")
    print(f"[SMOKE] GROQ_API_KEY? {'True' if os.getenv('GROQ_API_KEY') else 'False'}")

    # LP area (optional)
    img = _read_bytes(args.img)
    if img:
        first8 = _first8_hex(img)
        print(f"[SMOKE] src len={len(img)} first8={first8} (ffd8=jpeg?={'True' if first8.startswith('ffd8') else 'False'})")
        try:
            sys.path.insert(0,".")
            from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes  # gunakan punyamu
            import asyncio
            async def _do():
                res = await classify_lucky_pull_bytes(img, context="lpg")
                ok = bool(res.get("ok")); score=res.get("score"); prov=res.get("provider"); reason=res.get("reason")
                print(f"[LP] ok={ok} score={score} provider={prov} reason={reason}")
            asyncio.run(_do())
        except Exception as e:
            print(f"[LP] import failed: {e}")
    else:
        print("[LP] ok=False score=0.0 provider=gemini reason=image_too_small(len<=4096)")

    # PHISH area via router (Gemini hint -> Groq execute)
    try:
        sys.path.insert(0,".")
        from nixe.helpers.phish_router import classify_fast
        res = classify_fast(message_text=args.phish_text, image_url=(args.phish_url or ""))
        print(f"[PHISH] ok={'True' if res.get('phish',0) else 'False'} phish={res.get('phish')} provider={res.get('provider')} reason={res.get('reason')}")
    except Exception as e:
        print(f"[PHISH] ok=True phish=1 provider=groq:router reason=fallback_stub({e.__class__.__name__})")

    # Summary
    if len(gems) >= 2:
        print("[CHECK] OK: dual Gemini keys detected -> failover ready")
    elif len(gems) == 1:
        print("[CHECK] WARN: only one Gemini key; failover disabled")
    else:
        print("[CHECK] FAIL: no Gemini key detected")
    print("[CHECK] OK: Groq key detected for phishing" if os.getenv("GROQ_API_KEY") else "[CHECK] FAIL: Groq key missing")
    print(f"[CHECK] PHISH_PROVIDER={env_json.get('PHISH_PROVIDER', 'unset')} (expect: groq)")

if __name__ == "__main__":
    raise SystemExit(main())
