
import argparse, base64, json, os, time, mimetypes, sys, re, random
from typing import Dict, Any, Optional, Tuple, List
import requests

try:
    from scripts.smoke_utils import load_env_hybrid, read_json_tolerant, flatten_group_lines  # type: ignore
except Exception:
    sys.path.append(os.path.dirname(__file__))
    from smoke_utils import load_env_hybrid, read_json_tolerant, flatten_group_lines  # type: ignore

DISCORD_API = "https://discord.com/api/v10"
GEMINI_ENDPOINT_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
DEFAULT_THR = float(os.getenv("LPG_THRESHOLD", "0.85"))

def _headers() -> Dict[str, str]:
    token = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN (or DISCORD_BOT_TOKEN) in .env")
    return {"Authorization": f"Bot {token}", "User-Agent": "nixe-smoke/guard-online"}

def post_image_message(channel_or_thread_id: str, image_path: str, content: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    url = f"{DISCORD_API}/channels/{channel_or_thread_id}/messages"
    mime, _ = mimetypes.guess_type(image_path)
    if not mime:
        mime = "application/octet-stream"
    files = []
    with open(image_path, "rb") as f:
        files.append(("files[0]", (os.path.basename(image_path), f, mime)))
        payload = {"content": content or ""}
        data = {"payload_json": json.dumps(payload)}
        r = requests.post(url, headers=_headers(), data=data, files=files)
    if r.status_code in (200, 201):
        return True, r.json()["id"]
    return False, None

def delete_message(channel_or_thread_id: str, message_id: str) -> bool:
    url = f"{DISCORD_API}/channels/{channel_or_thread_id}/messages/{message_id}"
    r = requests.delete(url, headers=_headers())
    return r.status_code in (200, 202, 204)

def post_text(channel_or_thread_id: str, content: str) -> Tuple[bool, Optional[str]]:
    url = f"{DISCORD_API}/channels/{channel_or_thread_id}/messages"
    r = requests.post(url, headers=_headers(), json={"content": content})
    if r.status_code in (200, 201):
        return True, r.json()["id"]
    return False, None

def _candidate_persona_paths(p: str) -> List[str]:
    cands = []
    if p:
        cands += [p, p.replace('\\', '/'), p.replace('/', '\\')]
    cands += ["nixe/config/yandere.json", "nixe\\config\\yandere.json",
              "./nixe/config/yandere.json", "../nixe/config/yandere.json"]
    here = os.path.dirname(__file__)
    cands += [os.path.join(here, "yandere.json"),
              os.path.join(here, "../nixe/config/yandere.json")]
    seen, out = set(), []
    for x in cands:
        if x not in seen:
            out.append(x); seen.add(x)
    return out

def load_persona_line_groups(persona_path: str) -> Tuple[str, Optional[str]]:
    """
    Strict persona: only use lines from groups.* and render as-is.
    Returns (line, debug_err)
    """
    try:
        from nixe.helpers.persona_loader import load_persona, pick_line  # type: ignore
        data = load_persona(persona_path)
        line = pick_line(data, randomize=True)
        return line, None
    except Exception as e_first:
        errors = []
        for cand in _candidate_persona_paths(persona_path):
            try:
                if os.path.exists(cand):
                    data = read_json_tolerant(cand)
                    lines = flatten_group_lines(data)
                    if lines:
                        line = random.choice(lines)
                        return line, None
                    else:
                        errors.append(f"{cand}: no group lines")
                else:
                    errors.append(f"{cand}: not found")
            except Exception as e_json:
                errors.append(f"{cand}: {e_json!r}")
        return "(persona gagal dimuat)", "; ".join(errors) if errors else str(e_first)

def render_placeholders(line: str, user_id: Optional[str], channel_id: Optional[str], reason_txt: str = "Tebaran Garam") -> str:
    user_mention = f"<@{user_id}>" if user_id else "@user"
    chan_mention = f"<#{channel_id}>" if channel_id else "#channel"
    line = re.sub(r'\{user\}', user_mention, line, flags=re.I)
    line = re.sub(r'\{channel\}', chan_mention, line, flags=re.I)
    line = re.sub(r'\{reason\}', reason_txt, line, flags=re.I)
    return line

def classify_with_gemini(image_path: str,
                         model: str,
                         key_primary: Optional[str],
                         key_backup: Optional[str]) -> Dict[str, Any]:
    img_bytes = open(image_path, "rb").read()
    b64 = base64.b64encode(img_bytes).decode("ascii")
    mime, _ = mimetypes.guess_type(image_path)
    if not mime:
        mime = "image/png"
    prompt = (
        "You are a strict detector for gacha 'lucky pull' screenshots from games (e.g., HSR, Genshin, PGR, WuWa, etc.).\n"
        "Return a single JSON object with fields: lucky (true/false), score (0..1), reason (string).\n"
        "Be conservative: only lucky=true if it's clearly a results screen."
    )
    payload = {"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":mime,"data":b64}}]}],
               "generationConfig":{"temperature":0.0,"maxOutputTokens":256}}

    def _try(key: str) -> Dict[str, Any]:
        url = GEMINI_ENDPOINT_TMPL.format(model=model)
        r = requests.post(url, params={"key": key}, json=payload, timeout=25)
        if r.status_code != 200:
            return {"ok": False, "err": f"gemini_http_{r.status_code}", "text": r.text}
        data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            return {"ok": False, "err": "gemini_parse", "text": str(data)}
        i, j = text.find("{"), text.rfind("}")
        body = text[i:j+1] if (i>=0 and j>i) else "{}"
        try:
            obj = json.loads(body)
        except Exception:
            obj = {"lucky": False, "score": 0.0, "reason": "parse_failed"}
        return {"ok": True, "lucky": bool(obj.get("lucky", False)),
                "score": float(obj.get("score", 0.0)),
                "provider": f"gemini:{model}",
                "reason": str(obj.get("reason", "")) or "n/a"}

    if key_primary:
        res = _try(key_primary)
        if res.get("ok"):
            return res
    if key_backup:
        res = _try(key_backup)
        if res.get("ok"):
            return res
    return {"ok": False, "lucky": False, "score": 0.0, "provider": f"gemini:{model}", "reason": "no_success_key"}

def main():
    ap = argparse.ArgumentParser(description="Lucky Pull guard smoke (strict persona-only)")
    ap.add_argument("--img", required=True, help="Path to image file")
    ap.add_argument("--chan-id", help="Channel ID (parent)")
    ap.add_argument("--thread-id", help="Thread ID (if provided, used as target channel)")
    ap.add_argument("--user-id", help="User ID to mention (e.g., 228126085160763392)")
    ap.add_argument("--redirect", required=True, help="Channel ID for Lucky Pull redirect")
    ap.add_argument("--persona-file", default="nixe/config/yandere.json")
    ap.add_argument("--ttl", type=int, default=5)
    ap.add_argument("--dotenv", dest="dotenv_path")
    ap.add_argument("--runtime-json", dest="runtime_json", default="nixe\\config\\runtime_env.json")
    ap.add_argument("--log-chan-id", help="Optional: channel for debug logs")
    args = ap.parse_args()

    _ = load_env_hybrid(args.dotenv_path, args.runtime_json)

    target_id = args.thread_id or args.chan_id
    if not target_id:
        raise SystemExit("Provide --chan-id or --thread-id")
    if not os.path.exists(args.img):
        raise SystemExit(f"Image not found: {args.img}")

    ok, msg_id = post_image_message(target_id, args.img, content="(smoke: lucky-pull test)")
    if not ok or not msg_id:
        raise SystemExit("Failed to post test image")

    model = os.getenv("GEMINI_MODEL", "") or "gemini-2.5-flash-lite"
    keyA = os.getenv("GEMINI_API_KEY")
    keyB = os.getenv("GEMINI_API_KEY_B")
    res = classify_with_gemini(args.img, model, keyA, keyB)

    lucky = bool(res.get("lucky"))
    score = float(res.get("score", 0.0))
    provider = res.get("provider", "gemini")
    reason = res.get("reason", "")

    if lucky and score >= DEFAULT_THR:
        time.sleep(max(0, int(args.ttl)))
        delete_message(target_id, msg_id)

        # persona ONLY
        raw_line, perr = load_persona_line_groups(args.persona_file)
        rendered = render_placeholders(raw_line, args.user_id, args.redirect, reason_txt="Tebaran Garam")
        content = rendered  # no extra lines appended
        post_text(target_id, content)

        if perr and args.log_chan_id:
            post_text(args.log_chan_id, f"[smoke] persona load note: {perr}")
    else:
        post_text(target_id, f"[smoke] not lucky (score={score:.3f} via {provider}; reason={reason})")

if __name__ == "__main__":
    main()
