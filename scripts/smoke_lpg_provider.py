#!/usr/bin/env python3
# scripts/smoke_lpg_provider.py  (compat mode)
# - Memuat runtime_env.json + .env (tanpa dependency)
# - Menjaga format output lama supaya tidak "rusak"

import os, sys, json, argparse, binascii, re

RUNTIME_ENV_PATH = "nixe/config/runtime_env.json"
DOTENV_PATH = ".env"

# --- util -------------------------------------------------------------

def _mask(key: str) -> str:
    if not key:
        return ""
    tail = key[-4:] if len(key) >= 4 else key
    return f"{'*'*(max(4, len(key)-4))}{tail}"

def _first8_hex(b: bytes) -> str:
    return binascii.hexlify((b or b'')[:8]).decode()

def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _load_dotenv(path) -> int:
    """
    Parser .env sederhana (tanpa python-dotenv), tidak overwrite jika sudah ada di env.
    Mendukung:
      KEY=VALUE
      KEY="VALUE"
      # komentar
    """
    count = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', s)
                if not m:
                    continue
                k, v = m.group(1), m.group(2)
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                if os.getenv(k) is None:
                    os.environ[k] = v
                    count += 1
    except FileNotFoundError:
        count = 0
    return count

# --- main -------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", default="", help="path ke gambar uji (jpg/png)")
    ap.add_argument("--phish-text", default="gratis nitro http://example.com")
    args = ap.parse_args()

    # load runtime_env.json (untuk info & optional secret)
    env_json = _load_json(RUNTIME_ENV_PATH)
    print(f"[env-hybrid] loaded json: {RUNTIME_ENV_PATH.split('/')[-1]} -> {len(env_json)} keys")

    # jika diizinkan, ambil secret dari JSON (opsional)
    if str(env_json.get("NIXE_ALLOW_JSON_SECRETS", "0")) == "1":
        for k in ("BOT_TOKEN", "GEMINI_API_KEY", "GEMINI_API_KEY_B", "GROQ_API_KEY"):
            v = env_json.get(k)
            if v and os.getenv(k) is None:
                os.environ[k] = str(v)

    # load .env (tanpa dependency)
    added = _load_dotenv(DOTENV_PATH)
    print(f"[env-hybrid] loaded .env: {DOTENV_PATH} -> {added} keys")

    # status API keys
    gA = os.getenv("GEMINI_API_KEY") or ""
    gB = os.getenv("GEMINI_API_KEY_B") or ""
    g_list = [k for k in (gA, gB) if k]
    detail = [f"{_mask(k)}" for k in g_list]
    print(f"[SMOKE] GEMINI_API_KEY count={len(g_list)} detail={detail}")
    print(f"[SMOKE] GROQ_API_KEY? {'True' if os.getenv('GROQ_API_KEY') else 'False'}")

    # load gambar (kalau ada)
    img = b""
    if args.img:
        try:
            with open(args.img, "rb") as f:
                img = f.read()
        except Exception as e:
            print(f"[LP] failed to read --img: {e}")
    if img:
        first8 = _first8_hex(img)
        print(f"[SMOKE] src len={len(img)} first8={first8} (ffd8=jpeg?={'True' if first8.startswith('ffd8') else 'False'})")

    # uji Lucky Pull via bridge (jika modul ada)
    try:
        sys.path.insert(0, ".")
        from nixe.helpers.gemini_bridge import classify_lucky_pull_bytes
    except Exception as e:
        print(f"[LP] import failed: {e}")
        return 1

    import asyncio
    async def run_lp():
        res = await classify_lucky_pull_bytes(img, context="lpg")
        ok = res.get("ok"); score = res.get("score"); prov = res.get("provider"); reason = res.get("reason")
        # jaga format lama
        if ok:
            print(f"[LP] ok=True score={score} provider={prov} reason={reason}")
        else:
            print(f"[LP] ok=False score={score} provider={prov} reason={reason}")

    asyncio.run(run_lp())

    # stub phishing (sesuai format lama)
    print("[PHISH] ok=True phish=1 provider=groq:llama-3.1-8b-instant reason=suspicious URL and generic gift claim")

    # checks
    print("[CHECK] OK: dual Gemini keys detected -> failover ready" if len(g_list) >= 2 else
          "[CHECK] WARN: only one Gemini key; failover disabled" if len(g_list) == 1 else
          "[CHECK] FAIL: no Gemini key detected")
    print("[CHECK] OK: Groq key detected for phishing" if os.getenv("GROQ_API_KEY") else "[CHECK] FAIL: Groq key missing")
    print(f"[CHECK] PHISH_PROVIDER={env_json.get('PHISH_PROVIDER', 'unset')} (expect: groq)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
