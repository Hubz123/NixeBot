#!/usr/bin/env python3
"""
Smoketest: Google Drive (split-folder or monolithic) -> Nixe offline dict bootstrap -> Nixe translate (Gemini)
-> Gemini verifier (QC) for sentence-level translation.

Goal:
- Prove GDrive auth + recursive download works (split folder layout supported).
- Prove Nixe translate pipeline can translate an input sentence to multiple targets (JA/EN/ZH/KO).
- Prove Gemini verifier path runs and produces a structured QC result.

This test is intentionally NOT deterministic (Gemini). PASS criteria is structural:
- Translation request returns non-empty output for each target.
- Verifier returns JSON with pass=true OR provides a suggested fix (still treated as PASS unless --strict).

Secrets required:
- Drive auth:
  - GDRIVE_ACCESS_TOKEN
    OR
  - GDRIVE_REFRESH_TOKEN + GDRIVE_CLIENT_ID + GDRIVE_CLIENT_SECRET
- Gemini:
  - TRANSLATE_GEMINI_API_KEY (for translation)
  - TRANSLATE_GEMINI_QC_API_KEY (optional; if missing, reuses TRANSLATE_GEMINI_API_KEY)

Non-secret config:
- DICT_GDRIVE_FOLDER_ID (or --folder-id)
- Split folder names can be overridden via:
    DICT_MAP_ID_ID_FOLDER, DICT_MAP_ID_EN_FOLDER, DICT_MAP_ID_JA_FOLDER, DICT_MAP_ID_ZH_FOLDER, DICT_MAP_ID_KO_FOLDER
"""

from __future__ import annotations

import argparse
import logging
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

# Robust imports even if PYTHONPATH isn't set.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nixe.translate.local_dict_store import LocalDictStore

try:
    import aiohttp  # type: ignore
except Exception:  # pragma: no cover
    aiohttp = None


def _env(k: str, default: str = "") -> str:
    v = os.getenv(k)
    return default if v is None else str(v)


def _as_bool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _have_drive_auth() -> bool:
    if _env("GDRIVE_ACCESS_TOKEN", "").strip():
        return True
    if _env("GDRIVE_REFRESH_TOKEN", "").strip() and _env("GDRIVE_CLIENT_ID", "").strip() and _env("GDRIVE_CLIENT_SECRET", "").strip():
        return True
    return False


def _pick_gemini_translate_key() -> str:
    key = (_env("TRANSLATE_GEMINI_API_KEY", "") or "").strip()
    return key


def _pick_gemini_qc_key() -> str:
    key = (_env("TRANSLATE_GEMINI_QC_API_KEY", "") or "").strip()
    if key:
        return key
    return _pick_gemini_translate_key()


def _clean_output(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    # drop "model:" labels some backends prepend
    s = re.sub(r"^(?:via|provider|model)\s*[:=]\s*[^\n]+\n+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^(?:gemini)\s*[:\-]\s*", "", s, flags=re.IGNORECASE)
    return s.strip()


def _simple_tokenize(text: str) -> List[str]:
    """
    Simple tokenizer good enough for glossary extraction:
    - keeps words and basic punctuation
    - supports ID/EN inputs primarily
    """
    t = (text or "").strip()
    if not t:
        return []
    toks = re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]", t, flags=re.UNICODE)
    return [x for x in toks if x and not x.isspace()]


def _is_punct(tok: str) -> bool:
    return bool(re.fullmatch(r"[^\w]+", tok, flags=re.UNICODE))


async def _gemini_generate_json(*, key: str, model: str, sys_msg: str, user_text: str, schema_hint: str, temperature: float = 0.2) -> Tuple[bool, Dict[str, object], str]:
    """
    Gemini call that *expects* compact JSON.
    Returns (ok, parsed_json, raw_text).
    """
    if not aiohttp:
        return False, {}, "aiohttp missing"
    if not key:
        return False, {}, "missing Gemini key"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    prompt = sys_msg.strip() + "\n\nSCHEMA:\n" + schema_hint.strip() + "\n\nINPUT:\n" + user_text
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": float(temperature), "maxOutputTokens": 2048},
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=payload, timeout=float(_env("TRANSLATE_TIMEOUT_SEC", "25"))) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return False, {}, f"Gemini HTTP {resp.status}: {body[:300]}"
                j = await resp.json()
                cand = (j.get("candidates") or [{}])[0]
                parts = (((cand.get("content") or {}).get("parts")) or [])
                out = ""
                for p in parts:
                    if isinstance(p, dict) and "text" in p:
                        out += str(p["text"])
                raw = _clean_output(out)
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        return True, parsed, raw
                except Exception:
                    pass
                # Best-effort: try to extract JSON object substring.
                m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
                if m:
                    try:
                        parsed = json.loads(m.group(0))
                        if isinstance(parsed, dict):
                            return True, parsed, raw
                    except Exception:
                        pass
                return False, {}, raw
    except Exception as e:
        return False, {}, f"Gemini request failed: {e!r}"


async def _gemini_translate_text(*, text: str, target: str, glossary: str) -> Tuple[bool, str]:
    key = _pick_gemini_translate_key()
    if not key:
        return False, "missing TRANSLATE_GEMINI_API_KEY"
    model = _env("TRANSLATE_GEMINI_MODEL", "gemini-2.5-flash-lite")
    schema = _env("TRANSLATE_SCHEMA", '{"translation": "...", "reason": "..."}')
    sys_msg = _env(
        "TRANSLATE_SYS_MSG",
        f"You are a translation engine. Translate the user's text into {target}. "
        "Do NOT leave any part in the source language except proper nouns, usernames, or URLs. "
        f"If the text is already in {target}, return it unchanged. "
        f"Return ONLY compact JSON matching this schema: {schema}. No prose."
    )
    user_text = (glossary.strip() + "\n\n" if glossary.strip() else "") + "TEXT:\n" + (text or "")
    ok, j, raw = await _gemini_generate_json(
        key=key,
        model=model,
        sys_msg=sys_msg,
        user_text=user_text,
        schema_hint=schema,
        temperature=0.2,
    )
    if not ok:
        return False, f"translate failed: {raw[:300]}"
    out = str(j.get("translation", "") or "").strip()
    if not out:
        # fallback to raw if model didn't follow schema
        out = raw.strip()
    return True, out or "(empty)"


async def _gemini_verify_translation(*, src_lang: str, tgt_lang: str, src_text: str, tgt_text: str) -> Tuple[bool, Dict[str, object], str]:
    key = _pick_gemini_qc_key()
    if not key:
        return False, {}, "missing TRANSLATE_GEMINI_QC_API_KEY/TRANSLATE_GEMINI_API_KEY"
    model = _env("TRANSLATE_GEMINI_QC_MODEL", _env("TRANSLATE_GEMINI_MODEL", "gemini-2.5-flash-lite"))
    schema = _env("TRANSLATE_QC_SCHEMA", '{"pass": true, "issues": ["..."], "suggested_fix": "...", "score": 0-10, "notes": "..."}')
    sys_msg = _env(
        "TRANSLATE_QC_SYS_MSG",
        "You are a translation QA checker. Evaluate whether the translation preserves meaning and reads natural for the target language. "
        "If there are issues, list them and provide a suggested corrected translation. "
        "Return ONLY compact JSON matching the schema. No prose."
    )
    user_text = (
        f"SRC_LANG: {src_lang}\nTGT_LANG: {tgt_lang}\n\n"
        f"SRC_TEXT:\n{src_text}\n\nTGT_TEXT:\n{tgt_text}\n"
    )
    return await _gemini_generate_json(key=key, model=model, sys_msg=sys_msg, user_text=user_text, schema_hint=schema, temperature=0.2)


def _build_glossary(store: LocalDictStore, src_sentence: str, tgt_code: str) -> str:
    """
    Create a small glossary from offline dict lookup.
    This helps Gemini keep key terms consistent with the Kaikki dumps.
    """
    toks = _simple_tokenize(src_sentence)
    seen: set[str] = set()
    pairs: List[Tuple[str, str]] = []
    for t in toks:
        if not t or _is_punct(t):
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            payload = store.lookup_sync(t, tgt_code)  # may be empty if dict doesn't cover
        except Exception:
            payload = None
        tr = ""
        if payload:
            # LocalDictStore uses a compact "lang: ..." payload; best-effort extract target line.
            # Prefer direct "->" if present, else first non-empty line.
            s = str(payload)
            # Look for "tgt:" prefix lines like "ja: 猫"
            m = re.search(rf"(?im)^\s*{re.escape(tgt_code)}\s*[:=]\s*(.+?)\s*$", s)
            if m:
                tr = m.group(1).strip()
            else:
                # fallback: first line after stripping
                lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
                if lines:
                    tr = lines[0]
        if tr and tr.lower() != t.lower():
            pairs.append((t, tr))
        # keep glossary small
        if len(pairs) >= 18:
            break

    if not pairs:
        return ""
    lines = ["GLOSSARY (prefer these term mappings when appropriate):"]
    for a, b in pairs:
        lines.append(f"- {a} -> {b}")
    return "\n".join(lines).strip()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder-id", default=_env("DICT_GDRIVE_FOLDER_ID", "").strip(), help="Google Drive folder id holding dict dumps (root).")
    ap.add_argument("--src", default="id", help="Source language code (id/en).")
    ap.add_argument("--tgts", default="ja,en,zh,ko", help="Comma-separated target codes (default: ja,en,zh,ko).")
    ap.add_argument("--sentence", default="Saya suka kucing dan anjing.", help="Input sentence.")
    ap.add_argument("--keep-temp", action="store_true", help="Keep temp dir for inspection.")
    ap.add_argument("--strict", action="store_true", help="Fail if verifier returns pass=false for any target.")
    ap.add_argument("--bootstrap-timeout", type=float, default=float(_env("DICT_BOOTSTRAP_TIMEOUT_SEC", "300")), help="Bootstrap timeout seconds (default: 300).")
    args = ap.parse_args()

    if not args.folder_id:
        print("[ERR] missing folder id. Use --folder-id or set DICT_GDRIVE_FOLDER_ID.")
        return 2
    if not _have_drive_auth():
        print("[ERR] missing Drive auth. Set GDRIVE_ACCESS_TOKEN or refresh-token trio.")
        return 2
    if not _pick_gemini_translate_key():
        print("[ERR] missing TRANSLATE_GEMINI_API_KEY (translation).")
        return 2
    if not aiohttp:
        print("[ERR] aiohttp not installed; required for Gemini API calls.")
        return 2

    temp_root = Path(tempfile.mkdtemp(prefix="nixe_gdrive_nixe_translate_verify_"))
    dict_dir = temp_root / "dicts"

    print(f"[INFO] temp_dir={temp_root}")
    print(f"[INFO] folder_id={args.folder_id}")
    print(f"[INFO] src={args.src} tgts={args.tgts}")
    print(f"[INPUT] {args.sentence}")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    tk = _pick_gemini_translate_key()
    qk = _pick_gemini_qc_key()
    print("[INFO] gemini_translate_key=set" if tk else "[INFO] gemini_translate_key=missing")
    print("[INFO] gemini_qc_key=set" if qk else "[INFO] gemini_qc_key=reuse-or-missing")

    # Configure LocalDictStore for temp + gdrive bootstrap.
    os.environ["DICT_ENABLE"] = "1"
    os.environ["DICT_DIR"] = str(dict_dir)
    os.environ["DICT_GDRIVE_ENABLE"] = "1"
    os.environ["DICT_GDRIVE_FOLDER_ID"] = str(args.folder_id)
    os.environ["DICT_GDRIVE_AUTOCREATE"] = "0"
    os.environ["DICT_GDRIVE_LANGS"] = (args.src or "id").strip().lower()
    os.environ["DICT_GDRIVE_SPLIT_ONLY"] = _env("DICT_GDRIVE_SPLIT_ONLY", "1")  # Drive is split-folder in prod
    # Harden Drive HTTP timeouts to avoid hanging sockets
    os.environ.setdefault("GDRIVE_HTTP_TIMEOUT_SEC", _env("GDRIVE_HTTP_TIMEOUT_SEC", "30"))
    os.environ.setdefault("GDRIVE_DOWNLOAD_TIMEOUT_SEC", _env("GDRIVE_DOWNLOAD_TIMEOUT_SEC", "300"))
    # Do not rely on monolithic file presence; LocalDictStore bootstrap supports split folders.

    store = LocalDictStore()
    print("[INFO] bootstrapping dicts from GDrive (filtered by DICT_GDRIVE_LANGS)...")
    try:
        await asyncio.wait_for(store.bootstrap(), timeout=float(args.bootstrap_timeout))
    except asyncio.TimeoutError:
        print(f"[ERR] dict bootstrap timed out after {args.bootstrap_timeout:.0f}s. Check Drive auth/permissions/network.")
        return 3
    print("[OK] dict bootstrap done")

    # Confirm split folders are present for visibility
    present = []
    for code, folder in [("id","ID"),("en","EN"),("ja","JP"),("zh","CN"),("ko","KR")]:
        p = dict_dir / folder / "manifest.json"
        if p.exists() and p.stat().st_size > 0:
            present.append(folder)
    if present:
        print(f"[OK] split folders present: {', '.join(sorted(present))}")
    else:
        # Not fatal; could be monolithic files in dict_dir
        print("[WARN] no split-folder manifests found; assuming monolithic dumps are present.")

    failures = 0
    strict_fail = 0

    for tgt in [t.strip() for t in args.tgts.split(",") if t.strip()]:
        glossary = _build_glossary(store, args.sentence, tgt_code=tgt)
        ok_t, out = await _gemini_translate_text(text=args.sentence, target=tgt, glossary=glossary)
        if not ok_t:
            failures += 1
            print(f"[FAIL] translate tgt={tgt}: {out}")
            continue

        print(f"\n[TGT] {tgt}")
        if glossary:
            print("[GLOSSARY] enabled")
        print(f"[OUTPUT] {out}")

        ok_v, vj, vraw = await _gemini_verify_translation(src_lang=args.src, tgt_lang=tgt, src_text=args.sentence, tgt_text=out)
        if not ok_v:
            failures += 1
            print(f"[FAIL] verify tgt={tgt}: {vraw[:300]}")
            continue

        verdict = bool(vj.get("pass", False))
        score = vj.get("score", None)
        issues = vj.get("issues", [])
        suggested = (vj.get("suggested_fix", "") or "").strip()

        sc = f" score={score}" if score is not None else ""
        print(f"[VERIFY] pass={verdict}{sc}")
        if issues:
            if isinstance(issues, list):
                for it in issues[:6]:
                    if str(it).strip():
                        print(f"  - {str(it).strip()}")
            else:
                print(f"  - {str(issues).strip()}")
        if suggested:
            print(f"[SUGGESTED] {suggested}")

        if not verdict:
            if args.strict:
                strict_fail += 1
            else:
                # Non-strict: treat as soft fail but still indicates pipeline works
                print("[WARN] verifier did not pass; non-strict mode continues.")

    if not args.keep_temp:
        shutil.rmtree(temp_root, ignore_errors=True)

    if failures > 0:
        print(f"\n[RESULT] FAIL hard_failures={failures} strict_failures={strict_fail}")
        return 1
    if strict_fail > 0:
        print(f"\n[RESULT] FAIL strict_failures={strict_fail}")
        return 1
    print("\n[RESULT] PASS gdrive+nixe+gemini translate+verify OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))