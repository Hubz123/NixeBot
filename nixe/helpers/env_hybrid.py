
# nixe/helpers/env_hybrid.py — STRICT HYBRID LOADER (restored)
# Priority: runtime_env.json for configs; .env ONLY for *_API_KEY/*_TOKEN (and similar secrets).
from __future__ import annotations
import os, json, logging, re
from pathlib import Path

LOG = logging.getLogger("nixe.helpers.env_hybrid")

def _repo_root() -> Path:
    here = Path(__file__).resolve()
    # .../nixe/helpers -> project root is 2 levels up
    return here.parent.parent.parent

def _read_json_with_fallback(main: Path) -> tuple[dict, str, int]:
    """
    Try to read main JSON; if it fails, try main.with_suffix('.fixed.json').
    Returns: (data, used_path, err_code)  err_code=0 ok, 1 parse-fix used, 2 failed
    """
    def _load(p: Path):
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    used = str(main)
    try:
        data = _load(main)
        return data, used, 0
    except Exception as e1:
        LOG.warning("[env-hybrid] runtime_env.json parse failed: %r", e1)
        fx = main.with_suffix(".fixed.json")
        if fx.exists():
            try:
                data = _load(fx)
                used = str(fx)
                LOG.warning("[env-hybrid] using fallback: %s", fx)
                return data, used, 1
            except Exception as e2:
                LOG.error("[env-hybrid] fallback parse failed: %r", e2)
        # last resort: single missing-comma heuristic for a common case
        try:
            raw = main.read_text(encoding="utf-8", errors="ignore")
            fixed = re.sub(r'("LPG_DEDUP_DISABLE_LEGACY"\s*:\s*"1")\s*\n\s*("LPA_STRICT_MIN")', r"\1,\n  \2", raw)
            data = json.loads(fixed)
            used = str(main) + " (auto-fixed)"
            LOG.warning("[env-hybrid] applied auto-fix for missing comma after LPG_DEDUP_DISABLE_LEGACY")
            return data, used, 1
        except Exception:
            pass
        return {}, used, 2

def _load_env_file(root: Path) -> tuple[dict, str]:
    envp = os.getenv("ENV_FILE_PATH") or str(root / ".env")
    d = {}
    try:
        with open(envp, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                d[k.strip()] = v.strip()
        return d, envp
    except Exception:
        return {}, envp

def _export_env(cfg: dict, penv: dict) -> tuple[int, int]:
    """
    Export to process env:
    - All non-secret configs from runtime_env.json
    - ONLY *_API_KEY/*_TOKEN secrets from .env
    Returns: (#exported from json, #exported from env)
    """
    exp_json = exp_env = 0
    for k, v in cfg.items():
        if v is None:
            continue
        # Do not override any _API_KEY/_TOKEN here; keep secrets for .env
        if k.endswith("_API_KEY") or k.endswith("_TOKEN") or k.endswith("_SECRET"):
            continue
        os.environ[k] = str(v)
        exp_json += 1
    for k, v in penv.items():
        if k.endswith("_API_KEY") or k.endswith("_TOKEN") or k.endswith("_SECRET"):
            os.environ[k] = str(v)
            exp_env += 1
    return exp_json, exp_env

def load_hybrid() -> dict:
    root = _repo_root()
    # Where is runtime_env.json?
    rp = os.getenv("RUNTIME_ENV_PATH") or str(root / "nixe" / "config" / "runtime_env.json")
    cfg, used_json_path, err = _read_json_with_fallback(Path(rp))
    penv, envp = _load_env_file(root)
    exp_json, exp_env = _export_env(cfg, penv)

    status = {
        "runtime_env_json_path": used_json_path,
        "runtime_env_json_keys": len(cfg),
        "runtime_env_exported_total": exp_json,
        "runtime_env_tokens_skipped": sum(1 for k in cfg.keys() if k.endswith("_API_KEY") or k.endswith("_TOKEN") or k.endswith("_SECRET")),
        "env_file_path": envp,
        "env_file_keys": len(penv),
        "env_exported_tokens": sum(1 for k in penv.keys() if k.endswith("_API_KEY") or k.endswith("_TOKEN") or k.endswith("_SECRET")),
        "policy": "priority: runtime_env.json for configs; .env ONLY for *_API_KEY/*_TOKEN/*_SECRET",
        "GEMINI_API_KEY": bool(os.getenv("GEMINI_API_KEY")),
        "GROQ_API_KEY": bool(os.getenv("GROQ_API_KEY")),
        "DISCORD_TOKEN": bool(os.getenv("DISCORD_TOKEN")),
        "error": (None if err == 0 else ("fallback-used" if err == 1 else "parse-failed"))
    }
    return status
