
import os, aiohttp, json, base64

def _env(k: str, default: str = "") -> str:
    v = os.getenv(k)
    return v if v is not None and v != "" else default

async def classify_lucky_pull_bytes(image_bytes: bytes):
    key = _env("GEMINI_API_KEY", _env("GEMINI_API_KEY_B", _env("GEMINI_BACKUP_API_KEY", "")))
    if not key:
        return False, 0.0, "none", "no_api_key"
    model = _env("GEMINI_MODEL", "gemini-2.5-flash-lite")
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": 'Return ONLY JSON: {"lucky": true|false, "score": 0..1, "reason": "..."}'},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
            ]
        }],
        "generationConfig": {"temperature": 0.0, "topP": 0.0, "topK": 1, "candidateCount": 1, "maxOutputTokens": 128},
        "response_mime_type": "application/json"
    }
    timeout = 6.0
    async with aiohttp.ClientSession() as s:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        async with s.post(url, json=payload) as resp:
            txt = await resp.text()
            try:
                data = json.loads(txt)
                cand = data.get("candidates", [{}])[0]
                parts = ((cand.get("content") or {}).get("parts") or [])
                out = ""
                for p in parts:
                    if "text" in p: out += p["text"]
                obj = json.loads(out.strip())
                return bool(obj.get("lucky", False)), float(obj.get("score", 0.0)), f"gemini:{model}", str(obj.get("reason",""))
            except Exception:
                return False, 0.0, f"gemini:{model}", "parse_error"
