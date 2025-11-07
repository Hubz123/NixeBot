
from __future__ import annotations
import os, json, logging, random
from pathlib import Path
from typing import Optional, Dict

from discord.ext import commands

log = logging.getLogger(__name__)

def _read_env(name: str, default: str) -> str:
    v = os.getenv(name, default)
    return str(v).strip()

def _load_persona(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    with p.open('r', encoding='utf-8') as f:
        return json.load(f)

def _parse_weight_map() -> Dict[str, float]:
    # Accept multiple formats:
    # 1) PERSONA_TONE_DIST='{"soft":0.5,"agro":0.25,"sharp":0.25}'
    # 2) PERSONA_TONE_SOFT / _AGRO / _SHARP
    # 3) PERSONA_TONE='soft=0.5,agro=0.25,sharp=0.25'
    weights = {}
    dist = os.getenv("PERSONA_TONE_DIST","").strip()
    if dist:
        try:
            obj = json.loads(dist)
            if isinstance(obj, dict):
                weights.update({k.lower(): float(v) for k,v in obj.items()})
        except Exception:
            pass
    for envkey, tone in [("PERSONA_TONE_SOFT","soft"),
                         ("PERSONA_TONE_AGRO","agro"),
                         ("PERSONA_TONE_SHARP","sharp")]:
        val = os.getenv(envkey, "").strip()
        if val:
            try:
                weights[tone] = float(val)
            except Exception:
                pass
    pt = os.getenv("PERSONA_TONE","").strip()
    if "=" in pt or ":" in pt:
        for part in pt.split(","):
            if not part.strip(): 
                continue
            if "=" in part:
                k,v = part.split("=",1)
            elif ":" in part:
                k,v = part.split(":",1)
            else:
                continue
            try:
                weights[k.strip().lower()] = float(v.strip())
            except Exception:
                pass
    # Normalize
    total = sum(v for v in weights.values() if v > 0)
    if total > 0:
        weights = {k: max(0.0, float(v))/total for k,v in weights.items()}
    return weights

def _pick_tone_fixed_or_weighted(default_tone: str) -> str:
    # If PERSONA_TONE is fixed (soft/agro/sharp) -> return that
    tone = _read_env("PERSONA_TONE", default_tone).lower()
    if tone in ("soft","agro","sharp"):
        return tone
    if tone in ("auto","random","weighted") or any(ch in tone for ch in "=,:"):
        weights = _parse_weight_map()
        if not weights:
            # If nothing specified, equal distribution
            weights = {"soft": 1/3, "agro": 1/3, "sharp": 1/3}
        r = random.random()
        acc = 0.0
        for k, p in weights.items():
            acc += p
            if r <= acc:
                return k
        # Fallback
        return "soft"
    # Unknown string -> fallback to default
    return default_tone

def _pick_line_from_json(js: dict, tone: str = "soft") -> str:
    tone = (tone or "soft").lower()
    # 1) lucky subsection
    for key in ('lucky','lpg','notice'):
        node = js.get(key)
        if isinstance(node, dict):
            arr = node.get(tone) or node.get("soft") or node.get("lines")
            if isinstance(arr, list) and arr:
                return random.choice(arr)
    # 2) top-level tone
    arr = js.get(tone) or js.get("soft")
    if isinstance(arr, list) and arr:
        return random.choice(arr)
    # 3) generic
    arr = js.get("lines") or js.get("messages") or js.get("templates")
    if isinstance(arr, list) and arr:
        return random.choice(arr)
    # 4) any value
    for v in js.values():
        if isinstance(v, list) and v:
            return str(random.choice(v))
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "makasih ya, gunakan channel yang tepat~"

class PersonaRuntimeEnvOverlay(commands.Cog):
    """Persona picker honoring runtime_env with weighted tones."""
    def __init__(self, bot):
        self.bot = bot
        self.path = _read_env("PERSONA_FILE", _read_env("PERSONA_PROFILE_PATH", "nixe/config/yandere.json"))
        self.mode = _read_env("PERSONA_MODE", "yandere")
        try:
            self.persona_js = _load_persona(self.path)
            log.warning("[persona-wiring] file=%s mode=%s loaded", self.path, self.mode)
        except Exception as e:
            log.error("[persona-wiring] failed to load %s: %s", self.path, e)
            self.persona_js = {"soft": ["maaf ya, tolong pakai channel yang tepat."]}
        # Expose helper
        setattr(bot, "pick_persona_line_runtime_env", self.pick_line)

    def pick_line(self, tone: Optional[str] = None) -> str:
        # If tone fixed is provided by caller, use it; otherwise weighted/random per call
        t = tone or _pick_tone_fixed_or_weighted("soft")
        try:
            return _pick_line_from_json(self.persona_js, t)
        except Exception:
            return "oke, sudah kubereskan ya~"

async def setup(bot):
    await bot.add_cog(PersonaRuntimeEnvOverlay(bot))
