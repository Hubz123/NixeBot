from __future__ import annotations
import os, json, re
from pathlib import Path
from typing import Dict, Tuple

RUNTIME_JSON_CANDIDATES = [
    "nixe/config/runtime_env.json",
    "config/runtime_env.json",
    "nixe/runtime_env.json",
]

# ===== Policy =====
# - runtime_env.json is AUTHORITATIVE for configuration keys.
# - .env (or process env) is used ONLY for tokens/secret API keys.
#   Tokens are keys that match: *_API_KEY or *_TOKEN or in ALLOWED_TOKEN_KEYS below.
ALLOWED_TOKEN_KEYS = {
    "DISCORD_TOKEN", "BOT_TOKEN",
    "GEMINI_API_KEY", "GROQ_API_KEY",
    "GOOGLE_API_KEY", "OPENAI_API_KEY",
}
TOKEN_KEY_REGEX = re.compile(r".*(_API_KEY|_TOKEN)$", re.I)

def _is_token_key(k: str) -> bool:
    ku = k.upper()
    return (ku in ALLOWED_TOKEN_KEYS) or bool(TOKEN_KEY_REGEX.fullmatch(ku))

def _find_upwards(start: Path, name: str) -> Path|None:
    cur = start.resolve()
    for _ in range(14):
        p = cur / name
        if p.exists():
            return p
        if cur.parent == cur:
            break
        cur = cur.parent
    return None

def _find_runtime_json(start: Path) -> Path|None:
    for cand in RUNTIME_JSON_CANDIDATES:
        p = _find_upwards(start, cand)
        if p: return p
    return None

def _read_runtime_json(p: Path) -> Dict[str, str]:
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            out = {}
            for k, v in data.items():
                if v is None: 
                    continue
                if isinstance(v, (str, int, float, bool)):
                    out[str(k)] = str(v)
            return out
    except Exception:
        pass
    return {}

def _find_env_file(start: Path) -> Path|None:
    return _find_upwards(start, ".env")

def _parse_env_file(p: Path) -> Dict[str, str]:
    out: Dict[str,str] = {}
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            key = k.strip()
            val = v.strip().strip('"').strip("'")
            if key:
                out[key] = val
    except Exception:
        pass
    return out

def _export_env(kv: Dict[str,str], override: bool) -> int:
    c = 0
    for k, v in kv.items():
        if override or (k not in os.environ):
            os.environ[k] = v
            c += 1
    return c

def load_hybrid(start_dir: str|None=None) -> dict:
    """
    Merge policy:
    1) Export ALL config from runtime_env.json (authoritative) -> override=True for NON-token keys only.
       (Token-looking keys inside JSON will NOT overwrite existing environment.)
    2) Export ONLY token keys from .env (authoritative for tokens) -> override=True for token keys.
    3) Return a status dict for smoke/debugging.
    """
    start = Path(start_dir or os.getcwd())

    # 1) runtime_env.json
    rj_path = _find_runtime_json(start)
    rj_data = _read_runtime_json(rj_path) if rj_path else {}
    exported_json_all = 0
    exported_json_tokens_skipped = 0
    for k, v in rj_data.items():
        if _is_token_key(k):
            # do NOT override process env for tokens from JSON
            if k not in os.environ:
                os.environ[k] = v
                exported_json_all += 1
            else:
                exported_json_tokens_skipped += 1
        else:
            # configs from JSON are authoritative
            os.environ[k] = v
            exported_json_all += 1

    # 2) .env tokens only
    env_path = _find_env_file(start)
    env_data = _parse_env_file(env_path) if env_path else {}
    exported_env_tokens = 0
    for k, v in env_data.items():
        if _is_token_key(k):
            os.environ[k] = v  # override for tokens
            exported_env_tokens += 1
        # Non-token keys in .env are ignored by policy

    status = {
        "runtime_env_json_path": str(rj_path) if rj_path else None,
        "runtime_env_json_keys": len(rj_data),
        "runtime_env_exported_total": exported_json_all,
        "runtime_env_tokens_skipped": exported_json_tokens_skipped,
        "env_file_path": str(env_path) if env_path else None,
        "env_file_keys": len(env_data),
        "env_exported_tokens": exported_env_tokens,
        "policy": "priority: runtime_env.json for configs; .env ONLY for *_API_KEY/*_TOKEN",
        "GEMINI_API_KEY": bool(os.getenv("GEMINI_API_KEY")),
        "GROQ_API_KEY": bool(os.getenv("GROQ_API_KEY")),
        "DISCORD_TOKEN": bool(os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")),
    }
    return status
