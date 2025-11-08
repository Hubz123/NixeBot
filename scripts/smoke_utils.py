
import os, json, re
from typing import Optional, Dict, Any, List

def load_env_hybrid(dotenv_path: Optional[str] = None,
                    runtime_env_json_path: Optional[str] = None) -> Dict[str, Any]:
    summary = {
        "runtime_env_json_path": runtime_env_json_path or "nixe\\config\\runtime_env.json",
        "runtime_env_json_keys": 0,
        "runtime_env_exported_total": 0,
        "runtime_env_tokens_skipped": 0,
        "env_file_path": dotenv_path or ".env",
        "env_file_keys": 0,
        "env_exported_tokens": 0,
        "policy": "priority: runtime_env.json for configs; .env ONLY for *_API_KEY/*_TOKEN/*_SECRET",
        "GEMINI_API_KEY": False,
        "GROQ_API_KEY": False,
        "DISCORD_TOKEN": False,
        "error": None,
    }
    try:
        json_path = runtime_env_json_path or summary["runtime_env_json_path"]
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            summary["runtime_env_json_keys"] = len(data.keys())
            summary["runtime_env_exported_total"] = summary["runtime_env_json_keys"]
    except Exception as e:
        summary["error"] = f"runtime_env.json load error: {e!r}"
    try:
        env_path = dotenv_path or summary["env_file_path"]
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if (k.endswith("_API_KEY") or k.endswith("_TOKEN") or k.endswith("_SECRET")) and v:
                        os.environ.setdefault(k, v)
                        summary["env_exported_tokens"] += 1
                    summary["env_file_keys"] += 1
        summary["GEMINI_API_KEY"] = bool(os.getenv("GEMINI_API_KEY"))
        summary["GROQ_API_KEY"] = bool(os.getenv("GROQ_API_KEY"))
        summary["DISCORD_TOKEN"] = bool(os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN"))
    except Exception as e:
        summary["error"] = f".env load error: {e!r}"
    return summary

def read_json_tolerant(path: str):
    with open(path, 'r', encoding='utf-8-sig') as f:
        s = f.read()
    # strip comments
    import re
    s = re.sub(r'/\*.*?\*/', '', s, flags=re.S)
    s = re.sub(r'^\s*//.*$', '', s, flags=re.M)
    return json.loads(s)

def flatten_group_lines(data: dict) -> List[str]:
    """
    Given persona json with {"groups": {name: [lines]...}}, return all lines across groups.
    """
    lines = []
    groups = data.get("groups", {})
    if isinstance(groups, dict):
        for arr in groups.values():
            if isinstance(arr, list):
                lines.extend([x for x in arr if isinstance(x, str)])
    return lines
