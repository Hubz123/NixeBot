#!/usr/bin/env python3
import os, json, urllib.request, urllib.error, ssl
def _groq_req(url: str, data: dict, timeout: int = 12):
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("no_groq_api_key")
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)
def detect_phishing_text(text: str, timeout_sec: int = 12):
    model = os.getenv("GROQ_MODEL_TEXT", "llama-3.1-8b-instant")
    url = "https://api.groq.com/openai/v1/chat/completions"
    prompt = (
        "Classify if the following message is phishing or suspicious (1=yes,0=no). "
        "Respond strictly in JSON: {\"phish\":0|1, \"reason\":\"short\"}.\n"
        f"Message: ```{text}```"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a strict security classifier."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 64,
    }
    try:
        with _groq_req(url, payload, timeout=timeout_sec) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "provider": f"groq:{model}", "reason": f"http_error:{e.code}"}
    except Exception as e:
        return {"ok": False, "provider": f"groq:{model}", "reason": str(e)}
    out_text = ""
    try:
        out_text = data["choices"][0]["message"]["content"]
    except Exception:
        pass
    phish = 0; reason = "unparsed"
    if out_text:
        try:
            obj = json.loads(out_text)
            phish = int(obj.get("phish", 0))
            reason = str(obj.get("reason", "ok"))
        except Exception:
            low = out_text.lower()
            phish = 1 if ("phish" in low or "suspicious" in low) else 0
            reason = "fallback"
    return {"ok": True, "phish": phish, "provider": f"groq:{model}", "reason": reason}
