
# -*- coding: utf-8 -*-
import os, json

def _repo_root():
    here = os.path.abspath(os.path.dirname(__file__))
    return os.path.abspath(os.path.join(here, "..", ".."))

def candidate_paths():
    root = _repo_root()
    env_path = os.getenv("LPG_PERSONA_PATH") or os.getenv("PERSONA_PATH")
    cands = []
    if env_path:
        cands.append(env_path)
    cands.append(os.path.join(root, "nixe", "config", "yandere.json"))
    cands.append(os.path.join(root, "nixe", "config", "personas", "yandere.json"))
    cands.append(os.path.join(root, "nixe", "config", "lpg_persona.json"))
    return cands

def _normalize(data):
    """
    Accept both legacy:
        {"soft":[...],"agro":[...],"sharp":[...]}
    and v3 schema:
        {"version":3, "groups":{"soft":[...],"agro":[...],"sharp":[...]}, ...}
    Normalize to {"yandere": {"soft": [...], "agro": [...], "sharp": [...]}}.
    """
    try:
        # legacy flat schema
        if isinstance(data, dict) and {"soft","agro","sharp"} <= set(data.keys()):
            return {"yandere": {"soft": data.get("soft", []),
                                "agro": data.get("agro", []),
                                "sharp": data.get("sharp", [])}}
        # v3 grouped schema
        if isinstance(data, dict) and "groups" in data and isinstance(data["groups"], dict):
            g = data["groups"]
            return {"yandere": {"soft": g.get("soft", []) or [],
                                "agro": g.get("agro", []) or [],
                                "sharp": g.get("sharp", []) or []}}
    except Exception:
        pass
    return data

def load_persona():
    for p in candidate_paths():
        try:
            if not os.path.exists(p):
                continue
            with open(p, "r", encoding="utf-8") as f:
                raw = f.read()
            data = json.loads(raw)
            data = _normalize(data)
            if not isinstance(data, dict) or not data:
                continue
            mode = next(iter(data.keys()))
            block = data.get(mode) or {}
            if not isinstance(block, dict):
                continue
            if not {"soft","agro","sharp"} <= set(block.keys()):
                continue
            return mode, data, p
        except Exception:
            continue
    return None, {}, None

def pick_line(data, mode, tone, **kwargs):
    mode = mode if (mode in data) else (next(iter(data.keys())) if data else "yandere")
    tones = data.get(mode, {})
    tone = tone if tone in ("soft","agro","sharp") else "soft"
    bucket = tones.get(tone) or tones.get("soft") or []
    if not bucket:
        return "..."
    import random
    return random.choice(bucket)
