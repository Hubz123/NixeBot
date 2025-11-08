#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_runtime_providers_introspect.py
- Auto-add repo root (dir containing 'nixe/') to sys.path
- Reads nixe/config/runtime_env.json (export non-secret envs)
- Imports nixe.cogs.lucky_pull_guard
- Lists classify-like methods; optionally calls one with image bytes

Usage:
  python scripts/smoke_runtime_providers_introspect.py --list
  python scripts/smoke_runtime_providers_introspect.py --method classify --img path\to\img.png
  python scripts/smoke_runtime_providers_introspect.py  # best-effort auto-pick
"""
from __future__ import annotations
import os, sys, json, argparse, asyncio, time, importlib
from pathlib import Path

def _bootstrap_syspath():
    here = Path(__file__).resolve()
    for parent in [here.parent] + list(here.parents)[:6]:
        if (parent / "nixe").exists():
            sys.path.insert(0, str(parent)); return str(parent)
    for parent in list(here.parents)[:6]:
        for name in ("NixeBot","Nixe"):
            cand = parent / name
            if (cand / "nixe").exists():
                sys.path.insert(0, str(cand)); return str(cand)
    return None

ROOT = _bootstrap_syspath()
print(f"[PATH] repo_root = {ROOT or '(not found)'}")

def _load_env():
    for p in ("nixe/config/runtime_env.json","config/runtime_env.json"):
        if os.path.exists(p):
            try:
                data = json.load(open(p,"r",encoding="utf-8"))
                for k,v in data.items():
                    if isinstance(k,str) and not k.upper().endswith(("_API_KEY","_TOKEN","_SECRET")):
                        os.environ.setdefault(k, str(v))
                return p, len(data)
            except Exception as e:
                print("[WARN] env_load:", e)
                return p, 0
    return None, 0

def _png_1x1():
    import zlib, struct, binascii
    w,h=1,1; raw=b'\x00\x00\x00'
    sig=b'\x89PNG\r\n\x1a\n'
    def chunk(tag,data):
        body=tag+data; return struct.pack(">I",len(data))+body+struct.pack(">I",binascii.crc32(body)&0xffffffff)
    ihdr=chunk(b'IHDR', struct.pack(">2I5B",w,h,8,2,0,0,0))
    idat=chunk(b'IDAT', zlib.compress(b'\x00'+raw))
    iend=chunk(b'IEND', b'')
    return sig+ihdr+idat+iend

def _read_bytes(p):
    if not p: return _png_1x1()
    with open(p,"rb") as f: return f.read()

def _list_methods(inst):
    import inspect
    out=[]
    for name in dir(inst):
        if name.startswith("__"): continue
        fn=getattr(inst,name)
        if callable(fn):
            try:
                sig=str(inspect.signature(fn))
            except Exception:
                sig="(?)"
            tag="async" if inspect.iscoroutinefunction(fn) else "func"
            if any(k in name.lower() for k in ["class","lucky","burst","gem","detect","analy","run"]):
                out.append(f"{name}{sig} [{tag}]")
    return sorted(out)

async def _auto_call(inst, img_bytes, method_name=None):
    cand = []
    for name in ["_classify","classify","classify_image","classify_bytes","classify_content","classify_lucky",
                 "burst","gemini_burst","detect","analyze","run"]:
        fn = getattr(inst,name,None)
        if callable(fn): cand.append(name)
    if method_name:
        cand = [method_name] + [n for n in cand if n!=method_name]
    for name in cand:
        fn = getattr(inst,name,None)
        if not callable(fn): continue
        try:
            return name, await fn(img_bytes)
        except TypeError:
            try:
                return name, await fn({"bytes": img_bytes})
            except Exception as e:
                last = e
                continue
    raise AttributeError("No callable method succeeded; tried: "+", ".join(cand))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="List classify-like methods and exit")
    ap.add_argument("--method", help="Explicit method name to call (e.g., classify, burst, detect)")
    ap.add_argument("--img", help="Image path; if omitted, uses 1x1 PNG")
    args = ap.parse_args()

    env_path, keys = _load_env()
    print(f"[ENV] path={env_path} keys={keys} LPG_ALLOWED_PROVIDERS={os.getenv('LPG_ALLOWED_PROVIDERS') or os.getenv('LPG_LP_ALLOWED_PROVIDERS')}")

    mod = importlib.import_module("nixe.cogs.lucky_pull_guard")
    Guard = getattr(mod,"LuckyPullGuard",None)
    if Guard is None and hasattr(mod,"get_guard"):
        Guard = mod.get_guard()
    if Guard is None:
        raise RuntimeError("LuckyPullGuard not found in nixe.cogs.lucky_pull_guard")
    try:
        inst = Guard(bot=None)
    except TypeError:
        inst = Guard()

    methods = _list_methods(inst)
    if args.list or not methods:
        print("[METHODS]")
        for m in methods:
            print(" -", m)
        if args.list: return

    img = _read_bytes(args.img)
    t0=time.time()
    name, res = asyncio.get_event_loop().run_until_complete(_auto_call(inst, img, args.method))
    dt=round((time.time()-t0)*1000.0,1)
    print(f"[OK] called {name} in {dt} ms")
    print("Result:", res)

if __name__ == "__main__":
    main()
