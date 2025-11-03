# nixe/helpers/groq_bridge.py (v3: force-embed for Discord; robust downloader)
from __future__ import annotations
import os, json, base64
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode, quote

try:
    from groq import Groq
except Exception:
    Groq = None

try:
    import requests
except Exception:
    requests = None

try:
    from PIL import Image
    from io import BytesIO
except Exception:
    Image = None
    BytesIO = None

GROQ_VISION_DEFAULT = os.getenv("GROQ_MODEL_VISION", "llama-3.2-11b-vision-preview")
GROQ_TEXT_DEFAULT   = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_API_KEY        = os.getenv("GROQ_API_KEY") or ""

# New knobs
PHISH_FORCE_EMBED              = os.getenv("PHISH_FORCE_EMBED", "0") == "1"
PHISH_FORCE_EMBED_FOR_DISCORD  = os.getenv("PHISH_FORCE_EMBED_FOR_DISCORD", "1") == "1"

@dataclass
class PhishResult:
    ok: bool
    phish: int
    reason: str
    provider: str
    raw: Optional[Dict[str, Any]] = None

def _discord_png_variant(url: str) -> str:
    u = urlparse(url)
    host = u.netloc.lower()
    if host.endswith("discordapp.com") or host.endswith("discordapp.net"):
        new_host = "media.discordapp.net" if host == "cdn.discordapp.com" else host
        path = u.path
        if not path.lower().endswith(".png"):
            parts = path.rsplit("/", 1)
            if len(parts) == 2 and parts[1]:
                base = parts[1].split(".", 1)[0]
                path = parts[0] + "/" + base + ".png"
        qs = parse_qs(u.query)
        qs["format"] = ["png"]
        qs.setdefault("name", ["large"])
        q = urlencode({k: (v[-1] if isinstance(v, list) else v) for k, v in qs.items()})
        return urlunparse((u.scheme or "https", new_host, path, "", q, ""))
    return url

def _proxy_candidates(original: str) -> List[str]:
    if not original: return []
    cands = [
        _discord_png_variant(original),
        f"https://images.weserv.nl/?url={quote(original, safe='')}&output=png",
    ]
    if original.startswith("https://"):
        cands += ["https://r.jina.ai/" + original, "https://r.jina.ai/http://" + original[len("https://"):]]
    elif original.startswith("http://"):
        cands += ["https://r.jina.ai/" + original, "https://r.jina.ai/https://" + original[len("http://"):]]
    else:
        cands += ["https://r.jina.ai/https://" + original]
    out, seen = [], set()
    for x in cands:
        if x and x not in seen:
            out.append(x); seen.add(x)
    return out

def _download_bytes(url: str) -> bytes:
    if not requests: return b""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/123.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": url
        }
        r = requests.get(url, timeout=12, headers=headers)
        r.raise_for_status()
        return r.content or b""
    except Exception:
        return b""

def _to_png_bytes(content: bytes) -> bytes:
    if not content:
        return b""
    if Image and BytesIO:
        try:
            img = Image.open(BytesIO(content))
            buf = BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            pass
    return content  # fallback (maybe already PNG)

def _download_to_data_url(url: str) -> (str, str):
    content = _download_bytes(url)
    if not content:
        return "", "download_empty"
    png = _to_png_bytes(content)
    return f"data:image/png;base64,{base64.b64encode(png).decode()}", ""

def _groq_client():
    if not (GROQ_API_KEY and Groq):
        return None
    try:
        return Groq(api_key=GROQ_API_KEY)
    except Exception:
        return None

_VISION_PROMPT = (
    "Classify if this IMAGE is phishing/scam bait. "
    "Consider: suspicious/shortened URLs, QR scams, credential bait, fake nitro/giveaway, crypto drain. "
    'Output strict JSON only: {"phish":0|1,"reason":"short"}'
)

_TEXT_PROMPT = (
    "You are a scam/phishing detector. Decide if the provided TEXT indicates a phishing attempt (gift bait, fake nitro, suspicious URL, wallet drain, QR code scam). "
    'Return strict JSON only: {"phish":0|1,"reason":"short"}'
)

def _extract_json(s: str) -> Dict[str, Any]:
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(s[i:j+1])
        except Exception:
            pass
    return {"phish": 0, "reason": "non_json_response"}

def classify_phish_image(*, image_url: Optional[str] = None, image_bytes: Optional[bytes] = None,
                         context_text: str = "", model: Optional[str] = None) -> PhishResult:
    provider = f"groq:{(model or GROQ_VISION_DEFAULT)}"
    client = _groq_client()
    if not client:
        return PhishResult(True, 1, "fallback_stub(no_key_or_sdk)", provider)

    base_text = {"type": "text", "text": _VISION_PROMPT + (f"\nContext: {context_text}" if context_text else "")}
    last_err = None

    # Decide whether to force embed (for Discord domains or global flag)
    force_embed = PHISH_FORCE_EMBED
    url_host = ""
    if image_url:
        url_host = urlparse(image_url).netloc.lower()
        if PHISH_FORCE_EMBED_FOR_DISCORD and ("discordapp" in url_host or "discordapp.net" in url_host or "discord" in url_host):
            force_embed = True

    # 1) If NOT force-embed, try URL candidates with input_image type
    if image_url and not force_embed:
        for cand in _proxy_candidates(image_url):
            try:
                content = [base_text, {"type": "input_image", "image_url": {"url": cand}}]
                resp = client.chat.completions.create(model=(model or GROQ_VISION_DEFAULT),
                                                      messages=[{"role":"user","content":content}], temperature=0)
                data = _extract_json(resp.choices[0].message.content or "")
                ph = int(bool(data.get("phish", 0))); rs = str(data.get("reason", "uncertain"))
                return PhishResult(True, ph, rs, provider, {"raw": data})
            except Exception as e:
                last_err = e
                continue

    # 2) Build data URL from bytes or by downloading
    data_url = ""
    dl_reason = ""
    if image_bytes:
        try:
            png = _to_png_bytes(image_bytes)
            data_url = f"data:image/png;base64,{base64.b64encode(png).decode()}"
        except Exception:
            pass
    elif image_url:
        data_url, dl_reason = _download_to_data_url(_discord_png_variant(image_url))

    if data_url:
        try:
            content = [base_text, {"type": "input_image", "image_url": {"url": data_url}}]
            resp = client.chat.completions.create(model=(model or GROQ_VISION_DEFAULT),
                                                  messages=[{"role":"user","content":content}], temperature=0)
            data = _extract_json(resp.choices[0].message.content or "")
            ph = int(bool(data.get("phish", 0))); rs = str(data.get("reason", "uncertain"))
            return PhishResult(True, ph, rs, provider, {"raw": data})
        except Exception as e:
            last_err = e

    # 3) Text-only fallback, report precise reason
    try:
        content = [{"role":"user","content":[{"type":"text","text": _TEXT_PROMPT + (f"\nTEXT: {context_text}" if context_text else "")}]}]
        resp = client.chat.completions.create(model=GROQ_TEXT_DEFAULT, messages=content, temperature=0)
        data = _extract_json(resp.choices[0].message.content or "")
        ph = int(bool(data.get("phish", 0)))
        extra = ("download_empty" if dl_reason == "download_empty" else "image_fetch_failed")
        rs = f"{extra} + text_only: {str(data.get('reason','uncertain'))}"
        return PhishResult(True, ph, rs, f"groq:{GROQ_TEXT_DEFAULT}", {"raw": data})
    except Exception:
        pass

    name = last_err.__class__.__name__ if last_err else "BadRequestError"
    return PhishResult(True, 1, f"fallback_stub({name})", provider)
