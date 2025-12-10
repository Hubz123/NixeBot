# nixe/cogs/a16_fix_groq_vision_overlay.py
import os, logging, inspect
from discord.ext import commands
PREFS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
]
def _clean_csv(v: str):
    parts = [(p or "").strip() for p in (v or "").split(",")]
    return [p for p in parts if p]
def _filter_bad(models):
    out = []
    for m in models:
        ml = m.lower()
        if "llama-3.2-11b-vision-preview" in ml or "llama-3.2-90b-vision-preview" in ml:
            continue
        out.append(m)
    return out
class FixGroqVision(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        po = (os.getenv("LPA_PROVIDER_ORDER") or "").replace(" ", "")
        if po:
            os.environ["LPA_PROVIDER_ORDER"] = po
        mv = (os.getenv("GROQ_MODEL_VISION") or os.getenv("LPG_GROQ_MODEL_VISION") or "").strip()
        if not mv or ("scout" in mv.lower()) or ("maverick" in mv.lower()):
            os.environ["GROQ_MODEL_VISION"] = PREFS[0]
        cand = _clean_csv(os.getenv("GROQ_MODEL_VISION_CANDIDATES") or "")
        cand = _filter_bad(cand)
        if not cand:
            cand = PREFS[:]
        os.environ["GROQ_MODEL_VISION_CANDIDATES"] = ",".join(cand)
        logging.getLogger(__name__).info(
            "[lpg-vision-fix] order=%s vision=%s candidates=%s",
            os.getenv("LPA_PROVIDER_ORDER"),
            os.getenv("GROQ_MODEL_VISION"),
            os.getenv("GROQ_MODEL_VISION_CANDIDATES")
        )
async def setup(bot):
    add = getattr(bot, "add_cog")
    res = add(FixGroqVision(bot))
    if inspect.isawaitable(res):
        await res
