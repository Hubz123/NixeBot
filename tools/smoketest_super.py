#!/usr/bin/env python3
"""NixeBot Super SmokeTest (v6) - super lengkap gate

- compileall
- JSON validity (runtime + minipc)
- LPG/PHISH wiring invariants
- IMPORT ALL modules under nixe/ (fail if any import error)
- optional COG setup(bot) dry-run (requires discord.py; no login)
- YouTube watchlist checks + verifier QUICK/FULL

Read-only tool.

Examples:
  py -3 tools/smoketest_super.py --offline
  py -3 tools/smoketest_super.py --super
  py -3 tools/smoketest_super.py --super --strict
"""

from __future__ import annotations

import argparse
import compileall
import importlib
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

VERSION = "v6"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

DEFAULT_RUNTIME = REPO_ROOT / "nixe" / "config" / "runtime_env.json"
DEFAULT_RUNTIME_MINIPC = REPO_ROOT / "nixe" / "config" / "runtime_env_minipc.json"
DEFAULT_WATCHLIST = REPO_ROOT / "data" / "youtube_wuwa_watchlist.json"
DEFAULT_STATE = REPO_ROOT / "data" / "youtube_wuwa_state.json"

LPG_MAIN = REPO_ROOT / "nixe" / "cogs" / "a00_lpg_thread_bridge_guard.py"
PHISH_MAIN = REPO_ROOT / "nixe" / "cogs" / "phish_groq_guard.py"
YOUTUBE_MAIN = REPO_ROOT / "nixe" / "cogs" / "a21_youtube_wuwa_live_announce.py"
LPA_BRIDGE = REPO_ROOT / "nixe" / "helpers" / "lpa_provider_bridge.py"

@dataclass
class Result:
    name: str
    ok: bool
    details: str = ""
    warning: bool = False

def _read_text(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")

def _read_json(p: pathlib.Path) -> Any:
    return json.loads(_read_text(p))

def _short(s: str, n: int = 260) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " ..."

def _sh(cmd: List[str], *, cwd: Optional[pathlib.Path] = None, timeout_s: int = 120) -> Tuple[int, str]:
    try:
        out = subprocess.check_output(cmd, cwd=str(cwd) if cwd else None, stderr=subprocess.STDOUT, timeout=timeout_s)
        return 0, out.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        out = (e.output or b"").decode("utf-8", errors="replace") if getattr(e, "output", None) else ""
        return 124, out + f"\n[TIMEOUT after {timeout_s}s]"
    except subprocess.CalledProcessError as e:
        return int(e.returncode or 1), (e.output or b"").decode("utf-8", errors="replace")
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"

def _print_header(title: str):
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)

def _passfail(results: List[Result], *, strict: bool) -> int:
    errors: List[Result] = []
    warns: List[Result] = []
    for r in results:
        if r.warning:
            warns.append(r)
            if strict:
                errors.append(r)
        elif not r.ok:
            errors.append(r)

    print("\n" + "-" * 80)
    print(f"SUMMARY: {len(errors)} error(s), {len(warns)} warning(s), {len(results)} check(s) | strict={strict}")
    for r in results:
        status = "PASS" if r.ok and not r.warning else ("WARN" if r.warning else "FAIL")
        print(f"- {status:<4} {r.name}: {_short(r.details, 180)}")
    return 0 if not errors else 2

def check_compileall() -> Result:
    ok = compileall.compile_dir(str(REPO_ROOT), quiet=1)
    return Result("compileall", bool(ok), "All .py files compiled" if ok else "compileall reported failures")

def check_json_valid(p: pathlib.Path, name: str) -> Result:
    if not p.exists():
        return Result(name, False, f"Missing file: {p}")
    try:
        _read_json(p)
        return Result(name, True, f"Valid JSON: {p}")
    except Exception as e:
        return Result(name, False, f"Invalid JSON: {p} -> {type(e).__name__}: {e}")

def check_runtime_categories(runtime_path: pathlib.Path) -> Result:
    if not runtime_path.exists():
        return Result("runtime categories", False, f"Missing: {runtime_path}")
    raw = _read_text(runtime_path)
    seps = [m.start() for m in re.finditer(r'"--- [^\n\r]+ ---"\s*:', raw)]
    if len(seps) < 5:
        return Result("runtime categories", False, f"Expected category separators; found {len(seps)}")
    spread_ok = (max(seps) - min(seps)) > (len(raw) * 0.30)
    if not spread_ok:
        return Result("runtime categories", False, "Category separators look clustered (likely header-only block)")
    return Result("runtime categories", True, f"Found {len(seps)} category separators; spread looks OK")

def check_wiring() -> List[Result]:
    res: List[Result] = []

    if LPG_MAIN.exists():
        t = _read_text(LPG_MAIN)
        res.append(Result("LPG wiring", not bool(re.search(r"\bGROQ_API_KEY\b", t)), "No GROQ_API_KEY in LPG main (OK)" if not re.search(r"\bGROQ_API_KEY\b", t) else "LPG main references GROQ_API_KEY (NOT allowed)"))
        res.append(Result("LPG keys", bool(("GEMINI_API_KEY" in t) or ("GEMINI_API_KEY_B" in t) or ("GEMINI_API_KEYS" in t)), "LPG main references GEMINI_* keys (OK)" if (("GEMINI_API_KEY" in t) or ("GEMINI_API_KEY_B" in t) or ("GEMINI_API_KEYS" in t)) else "LPG main missing GEMINI_* key references"))
    else:
        res.append(Result("LPG wiring", False, f"Missing: {LPG_MAIN}"))

    if PHISH_MAIN.exists():
        t = _read_text(PHISH_MAIN)
        bad = bool(re.search(r"\bGEMINI_API_KEY\b|\bGEMINI_API_KEY_B\b|\bGEMINI_API_KEYS\b", t))
        res.append(Result("Phish wiring", not bad, "No GEMINI_* keys in phish module (OK)" if not bad else "Phish module references GEMINI_* (NOT allowed)"))
        res.append(Result("Phish keys", bool(re.search(r"\bGROQ_API_KEY\b", t)), "Phish module references GROQ_API_KEY (OK)" if re.search(r"\bGROQ_API_KEY\b", t) else "Phish module missing GROQ_API_KEY reference"))
    else:
        res.append(Result("Phish wiring", False, f"Missing: {PHISH_MAIN}"))

    if LPA_BRIDGE.exists():
        t = _read_text(LPA_BRIDGE)
        forbidden = bool(
            re.search(r"os\.getenv\(\s*[\"']GROQ_API_KEY", t) or
            re.search(r"os\.getenv\(\s*[\"']GROQ_API_KEYS", t) or
            re.search(r"os\.getenv\(\s*[\"']GROQ_KEYS", t) or
            re.search(r"os\.environ\s*\[\s*[\"']GROQ_API_KEY", t) or
            re.search(r"os\.environ\s*\.get\(\s*[\"']GROQ_API_KEY", t)
        )
        res.append(Result("LPA bridge safety", not forbidden, "lpa_provider_bridge does not read GROQ env keys (OK)" if not forbidden else "lpa_provider_bridge reads GROQ env keys (NOT allowed)"))
        mapping_ok = ("nixe.helpers.gemini_bridge" in t) or ("from nixe.helpers import gemini_bridge" in t)
        res.append(Result("LPA bridge mapping", bool(mapping_ok), "Provider mapping includes nixe.helpers.gemini_bridge (OK)" if mapping_ok else "Could not confirm mapping to nixe.helpers.gemini_bridge"))
    else:
        res.append(Result("LPA bridge", False, f"Missing: {LPA_BRIDGE}"))

    if YOUTUBE_MAIN.exists():
        t = _read_text(YOUTUBE_MAIN)
        if "NIXE_YT_WUWA_NOTIFY_ROLE_ID" in t:
            res.append(Result("YouTube mentions", True, "YouTube notify role logic present (OK)"))
        else:
            res.append(Result("YouTube mentions", True, "No notify-role setting found (OK)"))
    else:
        res.append(Result("YouTube module", False, f"Missing: {YOUTUBE_MAIN}"))
    return res

def check_config(runtime_path: pathlib.Path) -> List[Result]:
    r: List[Result] = []
    try:
        cfg = _read_json(runtime_path)
        if not isinstance(cfg, dict):
            return [Result("runtime schema", False, f"runtime_env.json must be object; got {type(cfg).__name__}")]
    except Exception as e:
        return [Result("runtime schema", False, f"Cannot read runtime: {type(e).__name__}: {e}")]

    lpg_has = ("LPG_GEMINI_THRESHOLD" in cfg) or ("GEMINI_LUCKY_THRESHOLD" in cfg)
    if not lpg_has and ("LPG_GROQ_THRESHOLD" in cfg):
        r.append(Result("LPG threshold keys", True, "runtime has LPG_GROQ_THRESHOLD but missing LPG_GEMINI_THRESHOLD/GEMINI_LUCKY_THRESHOLD; LPG may fallback to defaults.", warning=True))
    elif not lpg_has:
        r.append(Result("LPG threshold keys", True, "runtime missing LPG_GEMINI_THRESHOLD/GEMINI_LUCKY_THRESHOLD; LPG may fallback to defaults.", warning=True))
    else:
        r.append(Result("LPG threshold keys", True, "runtime contains LPG_GEMINI_THRESHOLD/GEMINI_LUCKY_THRESHOLD (OK)"))

    phish_enable = str(cfg.get("PHISH_GROQ_ENABLE", "")).strip()
    if phish_enable and phish_enable != "1":
        r.append(Result("PHISH_GROQ_ENABLE", True, f"PHISH_GROQ_ENABLE={phish_enable!r} (disabled)", warning=True))
    else:
        r.append(Result("PHISH_GROQ_ENABLE", True, f"PHISH_GROQ_ENABLE={phish_enable or '1?'} (OK)"))
    return r

def _normalize_watchlist(obj: Any) -> Tuple[List[Dict[str, Any]], str]:
    if isinstance(obj, dict) and isinstance(obj.get("targets"), list):
        return [x for x in obj["targets"] if isinstance(x, dict)], "dict[target]"
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)], "list"
    if isinstance(obj, dict):
        for k in ("watchlist", "channels", "items", "data"):
            v = obj.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)], f"dict[{k}]"
    return [], f"unknown({type(obj).__name__})"

def check_youtube_watchlist(watchlist_path: pathlib.Path, state_path: pathlib.Path, *, verify_full: bool, verify_sleep: float, verify_limit: int, verify_timeout: int, strict_verify: bool) -> List[Result]:
    r: List[Result] = []
    if not watchlist_path.exists():
        return [Result("YT watchlist", False, f"Missing: {watchlist_path}")]
    try:
        wl_obj = _read_json(watchlist_path)
    except Exception as e:
        return [Result("YT watchlist JSON", False, f"Invalid JSON: {type(e).__name__}: {e}")]

    wl, schema = _normalize_watchlist(wl_obj)
    r.append(Result("YT watchlist entries", True, f"{len(wl)} entries (schema={schema}) (OK)" if wl else f"0 entries (schema={schema})", warning=not bool(wl)))

    seen = set()
    dups = 0
    for item in wl:
        key = (str(item.get("channel_id") or "").strip() or str(item.get("handle") or "").strip() or str(item.get("url") or "").strip())
        if not key:
            continue
        if key in seen:
            dups += 1
        else:
            seen.add(key)
    r.append(Result("YT watchlist duplicates", dups == 0, "No duplicates detected (OK)" if dups == 0 else f"Found {dups} duplicate identifiers"))

    if state_path.exists():
        try:
            _read_json(state_path)
            r.append(Result("YT state JSON", True, "State JSON valid (OK)"))
        except Exception as e:
            r.append(Result("YT state JSON", False, f"Invalid state JSON: {type(e).__name__}: {e}"))
    else:
        r.append(Result("YT state JSON", True, "State JSON missing (created on first run)", warning=True))

    verifier = REPO_ROOT / "verify_youtube_watchlist.py"
    if verifier.exists():
        # STRUCT verifier: fast, no-network (validates schema/urls/dups)
        cmd_struct = [sys.executable, str(verifier), "--mode", "struct"]
        code_s, out_s = _sh(cmd_struct, cwd=REPO_ROOT, timeout_s=30)
        if code_s == 0:
            r.append(Result("verify_youtube_watchlist.py (struct)", True, "Verifier STRUCT exited 0 (OK)"))
        else:
            msg_s = f"Verifier STRUCT failed (code={code_s}). Output: {_short(out_s, 220)}"
            if strict_verify:
                r.append(Result("verify_youtube_watchlist.py (struct)", False, msg_s))
            else:
                r.append(Result("verify_youtube_watchlist.py (struct)", True, msg_s, warning=True))

        # NETWORK verifier: optional (can be slow). Only run when --verify-watchlist-full is explicitly set.
        if verify_full:
            limit = verify_limit if verify_limit > 0 else 10
            sleep = str(verify_sleep)
            cmd = [sys.executable, str(verifier), "--mode", "network", "--sleep", str(sleep), "--limit", str(limit), "--timeout", str(verify_timeout)]
            # Network bound: give a bit more time but avoid hanging CI/smoketest.
            base_timeout = 120
            code, out = _sh(cmd, cwd=REPO_ROOT, timeout_s=max(base_timeout, verify_timeout + 10))
            if code == 0:
                r.append(Result("verify_youtube_watchlist.py (network)", True, "Verifier NETWORK exited 0 (OK)"))
            else:
                msg = f"Verifier NETWORK failed (code={code}). Output: {_short(out, 220)}"
                if strict_verify:
                    r.append(Result("verify_youtube_watchlist.py (network)", False, msg))
                else:
                    r.append(Result("verify_youtube_watchlist.py (network)", True, msg, warning=True))
    else:
        r.append(Result("verify_youtube_watchlist.py", True, "Verifier not present (skip)", warning=True))
    return r

def _iter_modules_under_nixe() -> List[str]:
    nixe_dir = REPO_ROOT / "nixe"
    mods: List[str] = []
    for p in nixe_dir.rglob("*.py"):
        rel = p.relative_to(REPO_ROOT)
        if rel.name == "__init__.py":
            continue
        if "__pycache__" in rel.parts:
            continue
        # Skip weird filenames like 'config_ids.patch.append.py' (not importable as package)
        if '.' in p.stem:
            continue
        mods.append(".".join(rel.with_suffix("").parts))
    mods.sort()
    return mods

def check_import_all_modules() -> Result:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    mods = _iter_modules_under_nixe()
    failed: List[str] = []
    for m in mods:
        try:
            importlib.import_module(m)
        except SystemExit as e:
            failed.append(f"{m}: SystemExit({e})")
        except Exception as e:
            failed.append(f"{m}: {type(e).__name__}: {e}")
    if failed:
        return Result("import all modules", False, f"{len(failed)} module(s) failed import. First: {failed[0]}")
    return Result("import all modules", True, f"Imported {len(mods)} module(s) under nixe/ (OK)")

def check_cog_setup_all(*, timeout_s: int) -> List[Result]:
    out: List[Result] = []
    try:
        import asyncio
        import discord
        from discord.ext import commands
    except Exception as e:
        return [Result("cog setup", True, f"discord.py not available; skipping: {type(e).__name__}: {e}", warning=True)]

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    cog_dir = REPO_ROOT / "nixe" / "cogs"
    cog_files = sorted([p for p in cog_dir.glob("*.py") if p.name != "__init__.py"])

    for p in cog_files:
        mod = "nixe.cogs." + p.stem
        try:
            m = importlib.import_module(mod)
        except Exception as e:
            out.append(Result(f"cog import {p.stem}", False, f"{type(e).__name__}: {e}"))
            continue

        setup = getattr(m, "setup", None)
        if not callable(setup):
            out.append(Result(f"cog setup {p.stem}", True, "No setup() (skip)", warning=True))
            continue

        try:
            intents = discord.Intents.none()
            intents.message_content = True
            # Avoid noisy "Guilds intent seems to be disabled" warnings during dry-run.
            intents.guilds = True
            bot = commands.Bot(command_prefix="!", intents=intents)
        except Exception as e:
            out.append(Result(f"cog setup {p.stem}", False, f"Cannot create Bot: {type(e).__name__}: {e}"))
            continue

        async def _close_aiohttp_sessions_best_effort() -> None:
            """Close any aiohttp.ClientSession objects attached to cogs/bot.

            The cog setup dry-run creates bots without actually logging in; if a cog
            creates a session during setup, failing to close it can trigger
            "Unclosed client session" warnings.
            """
            try:
                import aiohttp  # type: ignore
            except Exception:
                return

            def _maybe_sessions(obj):
                for attr in ("session", "_session", "http", "_http", "client", "_client"):
                    try:
                        s = getattr(obj, attr, None)
                    except Exception:
                        continue
                    yield s

            # cogs
            for _name, cog in list(getattr(bot, "cogs", {}).items()):
                for s in _maybe_sessions(cog):
                    try:
                        if isinstance(s, aiohttp.ClientSession) and not s.closed:
                            await s.close()
                    except Exception:
                        pass
            # bot
            for s in _maybe_sessions(bot):
                try:
                    if isinstance(s, aiohttp.ClientSession) and not s.closed:
                        await s.close()
                except Exception:
                    pass

        async def _run_setup():
            try:
                await setup(bot)
            finally:
                # Always cleanup even if setup() raises.
                try:
                    await _close_aiohttp_sessions_best_effort()
                except Exception:
                    pass
                try:
                    await bot.close()
                except Exception:
                    pass

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            task = loop.create_task(_run_setup())
            loop.run_until_complete(asyncio.wait_for(task, timeout=timeout_s))
            pending = asyncio.all_tasks(loop)
            for t in pending:
                if not t.done():
                    t.cancel()
            try:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            out.append(Result(f"cog setup {p.stem}", True, "setup() executed (OK)"))
        except Exception as e:
            out.append(Result(f"cog setup {p.stem}", False, f"{type(e).__name__}: {e}"))
        finally:
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass
    return out

def main() -> int:
    ap = argparse.ArgumentParser(description=f"NixeBot Super SmokeTest {VERSION} (super lengkap)")
    ap.add_argument("--offline", action="store_true", help="Offline checks (includes import all modules)")
    ap.add_argument("--super", action="store_true", help="Super lengkap (includes cog setup + FULL verify by default)")
    ap.add_argument("--strict", action="store_true", help="Treat WARN as FAIL")
    ap.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    ap.add_argument("--runtime-minipc", default=str(DEFAULT_RUNTIME_MINIPC))
    ap.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    ap.add_argument("--state", default=str(DEFAULT_STATE))

    ap.add_argument("--verify-watchlist-full", action="store_true", help="Run watchlist verifier FULL mode (may be slow)")
    ap.add_argument("--verify-sleep", type=float, default=0.2)
    ap.add_argument("--verify-limit", type=int, default=0)
    ap.add_argument("--verify-timeout", type=int, default=20)
    ap.add_argument("--strict-verify-watchlist", action="store_true")

    ap.add_argument("--cog-setup", action="store_true", help="Run setup(bot) for every cog (requires discord.py)")
    ap.add_argument("--cog-setup-timeout", type=int, default=20)

    args = ap.parse_args()

    _print_header("ENV / SYSTEM")
    print(f"SmokeTest: {VERSION}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Platform: {platform.platform()}")
    print(f"Repo root: {REPO_ROOT}")

    results: List[Result] = []
    _print_header("OFFLINE CHECKS")
    results.append(check_compileall())
    results.append(check_json_valid(pathlib.Path(args.runtime), "runtime_env.json"))
    results.append(check_json_valid(pathlib.Path(args.runtime_minipc), "runtime_env_minipc.json"))
    results.append(check_runtime_categories(pathlib.Path(args.runtime)))
    results.extend(check_wiring())
    results.extend(check_config(pathlib.Path(args.runtime)))

    results.append(check_import_all_modules())

    verify_full = bool(args.verify_watchlist_full)
    results.extend(check_youtube_watchlist(
        pathlib.Path(args.watchlist),
        pathlib.Path(args.state),
        verify_full=verify_full,
        verify_sleep=float(args.verify_sleep),
        verify_limit=int(args.verify_limit),
        verify_timeout=int(args.verify_timeout),
        strict_verify=bool(args.strict_verify_watchlist),
    ))

    if args.cog_setup or args.super:
        _print_header("COG SETUP DRY-RUN")
        results.extend(check_cog_setup_all(timeout_s=int(args.cog_setup_timeout)))

    return _passfail(results, strict=bool(args.strict))

if __name__ == "__main__":
    raise SystemExit(main())
