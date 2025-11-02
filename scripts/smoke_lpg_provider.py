#!/usr/bin/env python3
import os, sys, json, re, argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def mask_tail(s: str, n=4):
    s = str(s or "")
    return ("*" * max(0, len(s)-n)) + s[-n:]

def load_envhybrid():
    candidates = [ROOT / "runtime_env.json", ROOT / "nixe" / "config" / "runtime_env.json"]
    loaded = False
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                cnt = 0
                for k, v in data.items():
                    if not isinstance(v, str):
                        try:
                            v = json.dumps(v, ensure_ascii=False)
                        except Exception:
                            v = str(v)
                    if k not in os.environ:
                        os.environ[k] = v
                        cnt += 1
                print(f"[env-hybrid] loaded json: {p.name} -> {cnt} keys")
                loaded = True
                break
            except Exception as e:
                print(f"[env-hybrid] loaded json: ERR {e}")
                break
    if not loaded:
        print("[env-hybrid] loaded json: not found")
    envp = ROOT / ".env"
    if envp.exists():
        cnt = 0
        for line in envp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
            if not m:
                continue
            k, v = m.group(1), m.group(2).strip()
            if v and len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            if k not in os.environ:
                os.environ[k] = v
                cnt += 1
        print(f"[env-hybrid] loaded .env: .env -> {cnt} keys")
    else:
        print("[env-hybrid] loaded .env: not found")

def split_keys(val: str):
    return [p for p in re.split(r'[\s,;|]+', val.strip()) if p] if val else []

def gather_gemini_keys():
    keys = []
    keys += split_keys(os.getenv("GEMINI_API_KEY", ""))
    keys += split_keys(os.getenv("GEMINI_API_KEY_B", ""))
    if not keys:
        raw = os.getenv("GEMINI_KEYS", "")
        if raw.startswith("["):
            try:
                arr = json.loads(raw)
                keys = [str(x).strip() for x in arr if str(x).strip()]
            except Exception:
                pass
        else:
            keys = [s.strip() for s in raw.split(",") if s.strip()]
    return keys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", help="image path for Lucky Pull test (optional)")
    ap.add_argument("--phish-text", default="Free Discord Nitro! Claim at http://discordnitro.gift.example now!")
    args = ap.parse_args()

    load_envhybrid()

    gkeys = gather_gemini_keys()
    groq = os.getenv("GROQ_API_KEY", "").strip()
    print(f"[SMOKE] GEMINI_API_KEY count={len(gkeys)} detail={[mask_tail(k) for k in gkeys]}")
    print(f"[SMOKE] GROQ_API_KEY? {'True' if groq else 'False'}")

    # Lucky Pull (Gemini)
    try:
        from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes
        img_bytes = b'\xff\xd8\xff\xe0fakejpeg'
        if args.img:
            try:
                img_bytes = Path(args.img).read_bytes()
            except Exception as e:
                print(f"[LP] failed to read --img: {e}")
        import asyncio
        async def _lp():
            try:
                res = await classify_lucky_pull_bytes(img_bytes, timeout_ms=int(os.getenv("GEMINI_TIMEOUT_MS", "20000")))
                print(f"[LP] ok={res.get('ok')} score={res.get('score')} provider={res.get('provider')} reason={res.get('reason')}")
            except Exception as e:
                print(f"[LP] classify failed: {e}")
        asyncio.run(_lp())
    except Exception as e:
        print(f"[LP] import failed: {e}")

    # Phishing (Groq)
    try:
        from nixe.helpers.groq_bridge import detect_phishing_text
        res = detect_phishing_text(args.phish_text)
        print(f"[PHISH] ok={res.get('ok')} phish={res.get('phish')} provider={res.get('provider')} reason={res.get('reason')}")
    except Exception as e:
        print(f"[PHISH] detect failed: {e}")

    if len(gkeys) >= 2:
        print("[CHECK] OK: dual Gemini keys detected -> failover ready")
    elif len(gkeys) == 1:
        print("[CHECK] WARN: only one Gemini key; failover disabled")
    else:
        print("[CHECK] FAIL: no Gemini key detected")

    if groq:
        print("[CHECK] OK: Groq key detected for phishing")
    else:
        print("[CHECK] FAIL: Groq key missing")

    prefer = os.getenv("PHISH_PROVIDER", "").lower()
    print(f"[CHECK] PHISH_PROVIDER={prefer or 'unset'} (expect: groq)")

if __name__ == "__main__":
    main()
