#!/usr/bin/env python3
import os, sys, io, json, time, argparse, base64, hashlib, math, mimetypes, requests, ast
from pathlib import Path

# ---------- util: env hybrid ----------
def _is_secret_key(name: str) -> bool:
    name = (name or '').upper().strip()
    # allow *_API_KEY, *_API_KEY_* (e.g., _API_KEY_B), *_BACKUP_API_KEY, *_TOKEN, *_SECRET
    return (
        name.endswith('_API_KEY') or
        name or
        name or
        name.endswith('_BACKUP_API_KEY') or
        ('_API_KEY_' in name)  # e.g. GEMINI_API_KEY_B
    )

def _read_env_hybrid(dotenv_path: str, runtime_json_path: str) -> dict:
    info = {
        "runtime_env_json_path": str(runtime_json_path),
        "env_file_path": str(dotenv_path),
        "runtime_env_json_keys": 0,
        "env_file_keys": 0,
        "runtime_env_exported_total": 0,
        "env_exported_tokens": 0,
        "GEMINI_API_KEY": False,
        "GEMINI_API_KEY_B": False,
        "DISCORD_TOKEN": bool(os.getenv("DISCORD_TOKEN")),
        "error": None,
    }
    # .env (only secrets)
    try:
        if dotenv_path and Path(dotenv_path).exists():
            txt = Path(dotenv_path).read_text(encoding="utf-8", errors="ignore")
            for line in txt.splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s: continue
                k, v = s.split("=", 1)
                k = k.strip(); v = v.strip()
                if len(v) >= 2 and ((v[0] == v[-1]) and v[0] in ("'", '"')):
                    v = v[1:-1]
                if k.endswith(("_API_KEY","_TOKEN","_SECRET")):
                    os.environ.setdefault(k, v)
            info["env_file_keys"] = len([ln for ln in txt.splitlines() if "=" in ln and not ln.strip().startswith("#")])
    except Exception as e:
        info["error"] = f".env read error: {e}"

    # runtime json (configs)
    try:
        data = json.loads(Path(runtime_json_path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            info["runtime_env_json_keys"] = len(data.keys())
            # non-secret hints (read-only); keep format intact
            for k,v in data.items():
                if isinstance(v, (str,int,bool)) and k in (
                    "LPG_GUARD_CHANNELS","LUCKYPULL_GUARD_CHANNELS","LPG_STATUS_THREAD_ID",
                    "LPG_PERSONA_FILE","PERSONA_FILE","PERSONA_PATH",
                    "LPG_NEGATIVE_TEXT","GEMINI_LUCKY_THRESHOLD","GEMINI_MODEL"
                ):
                    os.environ.setdefault(k, str(v))
    except Exception as e:
        info["error"] = f"runtime json read error: {e}"

    info["GEMINI_API_KEY"] = bool(os.getenv("GEMINI_API_KEY"))
    info["GEMINI_API_KEY_B"] = bool(os.getenv("GEMINI_API_KEY_B") or os.getenv("GEMINI_BACKUP_API_KEY"))
    info["DISCORD_TOKEN"] = bool(os.getenv("DISCORD_TOKEN"))
    return info

# ---------- parse helpers ----------
def _parse_negative_words() -> list:
    raw = os.getenv("LPG_NEGATIVE_TEXT", "").strip()
    words = []
    if not raw:
        return words
    try:
        # allow python-list-like or json list
        val = ast.literal_eval(raw)
        if isinstance(val, (list, tuple)):
            words = [str(x).strip().lower() for x in val if str(x).strip()]
        else:
            words = [w.strip().lower() for w in str(val).split(",") if w.strip()]
    except Exception:
        words = [w.strip().lower() for w in raw.split(",") if w.strip()]
    # dedup while preserving order
    seen = set(); uniq = []
    for w in words:
        if w not in seen:
            seen.add(w); uniq.append(w)
    return uniq

def _get_threshold(default_val: float = 0.95) -> float:
    envv = os.getenv("GEMINI_LUCKY_THRESHOLD", "").strip()
    try:
        if envv:
            return float(envv)
    except Exception:
        pass
    return default_val

# ---------- util: phash (simple aHash 64-bit) ----------
def _ahash64_bytes(b: bytes) -> int | None:
    try:
        from PIL import Image
        import numpy as np
        from io import BytesIO
        im = Image.open(BytesIO(b)).convert("L").resize((8,8))
        arr = np.asarray(im, dtype=np.float32)
        avg = arr.mean()
        bits = (arr > avg).astype("uint8")
        v = 0
        for i in range(64):
            v = (v << 1) | int(bits.flat[i])
        return int(v)
    except Exception:
        return None

# ---------- util: golden-color heuristic (only if result screen) ----------
def _golden_ratio_bytes(b: bytes) -> float:
    try:
        from PIL import Image
        import numpy as np
        from io import BytesIO
        im = Image.open(BytesIO(b)).convert("RGB")
        arr = np.asarray(im, dtype=np.uint8)
        r = arr[:,:,0].astype("int32"); g = arr[:,:,1].astype("int32"); bl = arr[:,:,2].astype("int32")
        mask = ((r > 200) & (g > 140) & (bl < 120) & ((r - bl) > 80) & ((r - g) < 120))
        ratio = float(mask.sum()) / float(arr.shape[0]*arr.shape[1])
        return ratio
    except Exception:
        return 0.0

def _heuristic_score_from_ratio(ratio: float) -> float:
    if ratio <= 0: return 0.0
    return min(0.98, ratio / 0.08)

# ---------- gemini call ----------
GEMINI_HOST = "https://generativelanguage.googleapis.com/v1beta"

def _build_prompt(neg_words: list) -> str:
    neg_txt = ", ".join(sorted(set(neg_words))) if neg_words else ""
    lines = [
        "Task: Decide if an image is a **gacha PULL RESULT screen** and if it is **lucky**.",
        "- 'Pull result screen' examples: a 10/11-pull grid with cards/tiles, 'NEW' badges, rarity stars near each card.",
        "- NOT result screens: loadout/deck/build/inventory/save-data, equipment lists, shop, mail, settings.",
    ]
    if neg_txt:
        lines.append("- Treat any image containing these UI phrases as NOT a result screen: " + neg_txt + ".")
    lines.extend([
        "- If NOT a result screen -> ok=false, score=0.0, reason='not_result_screen'.",
        "- If a result screen: lucky=true only if at least one top-tier/new reward (gold/orange banner, SSR/UR/Legendary, rainbow effect).",
        "Return ONLY compact JSON: {"is_result_screen": <true|false>, "ok": <true|false>, "score": <0..1>, "reason": "..."}. No prose.",
    ])
    return "\n".join(lines) + "\n"


def _gemini_request(model: str, api_key: str, image_bytes: bytes, prompt: str, timeout_s: float = 10.0) -> dict:
    url = f"{GEMINI_HOST}/models/{model}:generateContent?key={api_key}"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
            ]
        }],
        "generationConfig": {"temperature": 0.1, "topP": 0.1, "topK": 16}
    }
    r = requests.post(url, json=body, timeout=timeout_s)
    r.raise_for_status()
    return r.json()

def _gemini_parse(json_obj: dict):
    txt = ""
    try:
        cands = (json_obj.get("candidates") or [])
        for c in cands:
            parts = (((c.get("content") or {}).get("parts")) or [])
            for p in parts:
                if "text" in p:
                    txt += p["text"]
    except Exception:
        pass
    txt = (txt or "").strip()
    raw_text = txt
    is_result = False; ok = False; score = 0.0; reason = "empty"
    if txt:
        try:
            if txt.startswith("```"):
                idx = txt.find("{")
                if idx >= 0: txt = txt[idx:]
                txt = txt.strip("`")
            obj = json.loads(txt)
            is_result = bool(obj.get("is_result_screen"))
            ok = bool(obj.get("ok"))
            score = float(obj.get("score") or 0.0)
            reason = str(obj.get("reason") or "ok")
        except Exception:
            low = txt.lower()
            if any(w in low for w in ["save data","card count","obtained equipment","loadout","deck","inventory","profile"]):
                is_result = False; ok = False; score = 0.0; reason = "not_result_screen(text)"
            elif any(w in low for w in ["results","rescue","recruit","confirm","new badge","new badges"]) and any(w in low for w in ["gold","ssr","ur","legend","rainbow"]):
                is_result = True; ok = True; score = 0.7; reason = "result+rarity(text)"
            else:
                is_result = False; ok = False; score = 0.0; reason = "unparsed"
    return is_result, ok, max(0.0, min(1.0, score)), reason, raw_text

def classify_lucky(image_bytes: bytes, threshold: float = 0.95):
    neg_words = _parse_negative_words()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    api_a = os.getenv("GEMINI_API_KEY") or ""
    api_b = os.getenv("GEMINI_API_KEY_B") or os.getenv("GEMINI_BACKUP_API_KEY") or ""
    prompt = _build_prompt(neg_words)
    best = ("none", 0.0, "none", False, "")
    last_err = None
    for which, key in (("A", api_a), ("B", api_b)):
        if not key: continue
        try:
            js = _gemini_request(model, key, image_bytes, prompt, timeout_s=10.0)
            is_result, ok, sc, rs, raw = _gemini_parse(js)
            # local negative gate using configured words against raw model text
            low = (raw or "").lower()
            if any(w in low for w in neg_words):
                is_result = False; ok = False; sc = 0.0; rs = "neg_text_match"
            if sc > best[1]: best = (which, sc, rs, is_result, raw)
            if is_result and (sc >= threshold or ok):
                return True, sc, f"gemini:{model}[{which}]", rs, True
            if not is_result and which == "B":
                return False, 0.0, f"gemini:{model}[{which}]", "not_result_screen", False
        except requests.HTTPError as he:
            if he.response is not None and he.response.status_code == 429:
                last_err = f"429 on {which}"
                continue
            last_err = f"http error on {which}: {he}"
        except Exception as e:
            last_err = f"error on {which}: {e}"
            continue

    # Heuristic only if best guess suggests result screen
    if best[3]:
        ratio = _golden_ratio_bytes(image_bytes)
        hsc = _heuristic_score_from_ratio(ratio)
        if hsc >= threshold*0.85 and hsc >= 0.66:
            return True, hsc, "heuristic:gold-ratio", f"golden_ratio={ratio:.4f}", True
    prov = f"gemini:{model}[{best[0]}]" if best[0] != "none" else "none"
    if best[3] is False:
        return False, 0.0, prov, "not_result_screen", False
    return False, best[1], prov, (best[2] or last_err or "below_threshold"), best[3]

# ---------- discord post ----------
DISCORD_API = "https://discord.com/api/v10"
def _headers():
    tok = os.getenv("DISCORD_TOKEN")
    if not tok: raise SystemExit("Missing DISCORD_TOKEN")
    return {"Authorization": f"Bot {tok}", "User-Agent": "nixe-smoke/online"}

def post_image(channel_id: str, image_bytes: bytes, filename: str, content: str) -> str:
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    files = [("files[0]", (filename, io.BytesIO(image_bytes), mime))]
    data = {"payload_json": json.dumps({"content": content})}
    r = requests.post(url, headers=_headers(), data=data, files=files, timeout=30)
    r.raise_for_status()
    return r.json()["id"]

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--img", required=True, help="Path to image file")
    ap.add_argument("--chan-id", required=True, help="Channel ID to post to")
    ap.add_argument("--thread-id", required=True, help="Thread ID to post to (for logging cache)")
    ap.add_argument("--user-id", required=True, help="User ID to mention or attribute")
    ap.add_argument("--redirect", required=True, help="Redirect channel on success")
    ap.add_argument("--persona-file", required=False, help="Persona JSON path (not modified)")
    ap.add_argument("--ttl", type=int, default=15)
    ap.add_argument("--dotenv", default=".env")
    ap.add_argument("--runtime-json", default="nixe/config/runtime_env.json")
    ap.add_argument("--threshold", type=float, default=0.95, help="Lucky score threshold (default 0.95)")
    args = ap.parse_args()

    info = _read_env_hybrid(args.dotenv, args.runtime_json)
    # override threshold from runtime if set
    th = _get_threshold(args.threshold)
    print("=== ENV HYBRID CHECK ===")
    print(json.dumps({k:v for k,v in info.items() if k != "error"}, indent=2))
    if info.get("error"):
        print("[WARN]", info["error"], file=sys.stderr)

    with open(args.img, "rb") as f:
        b = f.read()

    print("\n=== GEMINI CLASSIFY ===")
    t0 = time.time()
    ok, score, provider, reason, is_result = classify_lucky(b, threshold=th)
    ok = bool(is_result and (score >= th))  # enforce runtime threshold
    dur = (time.time() - t0)*1000.0
    print(f"[RESULT] ok={ok} score={score:.3f} provider={provider} reason={reason} is_result={is_result} dur_ms={dur:.1f}")

    # pHash + cache (per thread)
    ph = _ahash64_bytes(b)
    cache_dir = Path("nixe/cache"); cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"lucky_phash_thread_{args.thread_id}.json"
    rec = {"ts": int(time.time()), "phash": (hex(int(ph))[2:].upper() if isinstance(ph,int) else None),
           "ok": ok, "score": float(score), "provider": provider, "reason": reason, "is_result": bool(is_result)}
    try:
        arr = []
        if cache_file.exists():
            arr = json.loads(cache_file.read_text("utf-8"))
            if not isinstance(arr, list): arr = []
        arr.append(rec)
        cache_file.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[CACHE] wrote {cache_file}")
    except Exception as e:
        print(f"[CACHE] skip ({e})")

    # Post marker message to channel (so guard can act); keep existing content format
    try:
        content = f"(smoke: lucky-pull test) [score={score:.3f} via {provider}; reason={reason}; is_result={is_result}]"
        mid = post_image(args.chan_id if args.chan_id else args.thread_id, b, Path(args.img).name, content=content)
        print(f"[POST] message id={mid}")
    except Exception as e:
        print(f"[POST] failed: {e}", file=sys.stderr)

    if ok and is_result:
        print(f"[smoke] lucky (score={score:.3f} via {provider}; reason={reason})")
        sys.exit(0)
    else:
        print(f"[smoke] not lucky (score={score:.3f} via {provider}; reason={reason})")
        sys.exit(0)

if __name__ == "__main__":
    main()
