import os, json
def get_env_json(path_candidates):
    for p in path_candidates:
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}
def read_key(k, runtime_json=None, default=None):
    if runtime_json and k in runtime_json: return runtime_json[k]
    v = os.getenv(k)
    return v if v is not None else default