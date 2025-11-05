#!/usr/bin/env python3

# -*- coding: utf-8 -*-
"""
smoke_runtime_providers.py
- Tujuan: memastikan jalur provider (Gemini/Groq) berjalan saat bot *run*,
  bukan hanya via helper terpisah.
- Cara kerja:
  * Import Cog `nixe.cogs.lucky_pull_guard` (seperti saat bot load).
  * Instansiasi Cog (tanpa connect ke Discord).
  * Panggil `_classify(img_bytes)` -> ini men-trigger bridge provider runtime.
  * Laporkan provider yang dipakai (gemini:..., groq:..., dll) + score/reason.
- Opsional:
  --img <path>           : path gambar lokal untuk dites (disarankan).
  --phish-url <url>      : unduh gambar dari URL (CDN Discord/HTTP).
  --timeout-ms <int>     : override timeout provider.
  --prefer <gemini|groq> : hint provider yang diharapkan (untuk PASS policy).

Exit code: 0 jika tes lolos, 1 jika gagal.
"""
from __future__ import annotations

# -*- coding: utf-8 -*-

import os, sys, io, json, argparse, time, urllib.request, importlib


def _export_env_from_files():
    """Load keys from runtime_env.json and .env into os.environ if not set."""
    # runtime_env.json
    jpath = os.path.join(ROOT, "nixe", "config", "runtime_env.json")
    try:
        with open(jpath, "r", encoding="utf-8") as f:
            for k, v in (json.load(f) or {}).items():
                if isinstance(v, (dict, list)):
                    continue
                if k not in os.environ and isinstance(v, (str, int, float)):
                    os.environ[k] = str(v)
    except Exception:
        pass
    # .env (very basic parser)
    envp = os.path.join(ROOT, ".env")
    try:
        if os.path.exists(envp):
            for line in open(envp, "r", encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path: sys.path.insert(0, ROOT)

def _load_env_json():
    path = os.path.join(ROOT, "nixe", "config", "runtime_env.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _read_bytes(img_path: str) -> bytes:
    with open(img_path, "rb") as f:
        b = f.read()
    return b

def _read_url(url: str, timeout=10) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent":"curl/8 smoke-runtime"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def main():
    _export_env_from_files()
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", help="lokasi file gambar lokal")
    ap.add_argument("--phish-url", help="URL gambar (akan di-download)")
    ap.add_argument("--timeout-ms", type=int, default=None)
    ap.add_argument("--prefer", choices=["gemini","groq"], default=None)
    args = ap.parse_args()

    envj = _load_env_json()
    dot_env_gemini = os.getenv("GEMINI_API_KEY","")
    dot_env_groq   = os.getenv("GROQ_API_KEY","")
    print(f"[ENV] GEMINI_API_KEY? {'YES' if dot_env_gemini else 'NO'} | GROQ_API_KEY? {'YES' if dot_env_groq else 'NO'}")

    # Import Cog persis seperti saat bot run
    try:
        mod = importlib.import_module("nixe.cogs.lucky_pull_guard")
        Guard = getattr(mod, "LuckyPullGuard")
        print("[OK] import nixe.cogs.lucky_pull_guard")
    except Exception as e:
        print("[FAIL] import lucky_pull_guard ->", repr(e)); sys.exit(1)

    # Dummy bot object hanya agar konstruktor tidak error
    class _DummyBot: pass
    bot = _DummyBot()

    try:
        guard = Guard(bot=bot)
        print("[OK] init LuckyPullGuard; providers=", getattr(guard, "provider_order", None))
        if args.timeout_ms:
            # Jika bridge membaca env, override via env var yg dipakai bridge
            os.environ["LUCKYPULL_GEM_TIMEOUT_MS"] = str(args.timeout_ms)
            os.environ["LPA_PROVIDER_TIMEOUT_MS"] = str(args.timeout_ms)
    except Exception as e:
        print("[FAIL] init guard ->", repr(e)); sys.exit(1)

    # Siapkan bytes gambar
    data = None
    if args.img:
        try:
            data = _read_bytes(args.img)
            print(f"[OK] read image bytes from {args.img} ({len(data)} bytes)")
        except Exception as e:
            print("[FAIL] read --img ->", repr(e)); sys.exit(1)
    elif args.phish_url:
        try:
            data = _read_url(args.phish_url, timeout=15)
            print(f"[OK] downloaded image from URL ({len(data)} bytes)")
        except Exception as e:
            print("[FAIL] read --phish-url ->", repr(e)); sys.exit(1)
    else:
        print("[WARN] no image provided; using 1x1 pixel fallback (provider will likely reject). PASS only checks provider wiring; for real test, provide --img/--phish-url.")
        data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0bIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfeA\x82\x93\x95\x00\x00\x00\x00IEND\xaeB`\x82"

    # Panggil classifier runtime
    import asyncio
    async def run():
        try:
            ok, score, provider, reason = await guard._classify(data)
        except TypeError:
            # fallback untuk implementasi lama (tuple tanpa await)
            res = guard._classify(data)
            if hasattr(res, "__await__"):
                res = await res
            if isinstance(res, (tuple, list)) and len(res)>=4:
                ok, score, provider, reason = res[:4]
            elif isinstance(res, dict):
                ok, score, provider, reason = bool(res.get("ok")), float(res.get("score") or 0.0), str(res.get("provider") or "unknown"), str(res.get("reason") or "")
            else:
                raise
        return ok, float(score), str(provider), str(reason)

    try:
        ok, score, provider, reason = asyncio.get_event_loop().run_until_complete(run())
    except RuntimeError:
        ok, score, provider, reason = asyncio.new_event_loop().run_until_complete(run())

    print(f"[RESULT] ok={ok} score={score:.3f} provider={provider} reason={reason}")

    # Kriteria PASS:
    # - Ada provider "gemini:*" atau "groq:*"
    # - Jika --prefer diset, provider harus sesuai
    vprovider = str(provider).lower()
    if ("gemini" in vprovider or "groq" in vprovider) and (args.prefer is None or args.prefer in vprovider):
        print("== SUMMARY == PASS (runtime provider active)")
        sys.exit(0)
    else:
        print("== SUMMARY == FAIL (provider not active or mismatched)")
        sys.exit(1)

if __name__ == "__main__":
    main()