#!/usr/bin/env python3
"""
Smoketest: Google Drive -> TEMP download dict dumps -> offline lookup for BOTH single words AND "full sentence" probes.

Why this exists:
- You want a test that is functionally similar to Render runtime: it proves Drive auth works, files can be fetched,
  and the dict lookup path can serve results.
- It MUST NOT persist dict files into repo; downloads go to a temp dir and are deleted at end (unless --keep-temp).

What "full sentence" means here:
- The offline dict is word-entry based (Wiktextract/Kaikki JSONL). It is not a full MT engine.
- For sentence-level smoke, we:
  1) print the sentence (so you can confirm the test case),
  2) probe a curated list of tokens expected to appear in the sentence (e.g., 猫, 学校, 行く),
  3) for space-delimited languages (en/id), we also probe whitespace tokens.
- PASS criteria: for each configured sentence, at least ONE probe token returns a non-empty lookup payload.

Secrets required (env):
- GDRIVE_ACCESS_TOKEN
  OR
- GDRIVE_REFRESH_TOKEN + GDRIVE_CLIENT_ID + GDRIVE_CLIENT_SECRET

Non-secret (env or args):
- DICT_GDRIVE_FOLDER_ID (or --folder-id)
- DICT_MAP_ID_{JA,KO,ZH,ID,EN}_FILE (or defaults)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

# Robust imports even if PYTHONPATH isn't set.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nixe.storage.gdrive import ensure_file_id, download_to_path
from nixe.translate.local_dict_store import LocalDictStore


def _env(k: str, default: str = "") -> str:
    v = os.getenv(k)
    return default if v is None else str(v)


def _have_drive_auth() -> bool:
    if _env("GDRIVE_ACCESS_TOKEN", "").strip():
        return True
    if _env("GDRIVE_REFRESH_TOKEN", "").strip() and _env("GDRIVE_CLIENT_ID", "").strip() and _env("GDRIVE_CLIENT_SECRET", "").strip():
        return True
    return False


def _dict_files_from_env() -> Dict[str, str]:
    return {
        "ja": _env("DICT_MAP_ID_JA_FILE", "ja-extract.jsonl.gz"),
        "ko": _env("DICT_MAP_ID_KO_FILE", "ko-extract.jsonl.gz"),
        "zh": _env("DICT_MAP_ID_ZH_FILE", "zh-extract.jsonl.gz"),
        "id": _env("DICT_MAP_ID_ID_FILE", "id-extract.jsonl.gz"),
        "en": _env("DICT_MAP_ID_EN_FILE", "raw-wiktextract-data.jsonl.gz"),
    }


# Single-word samples (quick sanity)
WORD_SAMPLES: Dict[str, List[Tuple[str, str]]] = {
    "ja": [("猫", "en"), ("犬", "en"), ("ありがとう", "en")],
    "ko": [("사랑", "en"), ("고양이", "en"), ("안녕하세요", "en")],
    "zh": [("你好", "en"), ("猫", "en"), ("谢谢", "en")],
    "id": [("kucing", "en"), ("anjing", "en"), ("terima kasih", "en")],
    "en": [("cat", "id"), ("dog", "id"), ("hello", "id")],
}

# Sentence cases + probe tokens (focus JP).
SENTENCE_CASES: Dict[str, List[Dict[str, object]]] = {
    "ja": [
        {
            "sentence": "私は猫が好きです。",
            "target": "en",
            "probe_tokens": ["猫", "好き", "私"],
        },
        {
            "sentence": "明日は学校に行きます。",
            "target": "en",
            "probe_tokens": ["明日", "学校", "行く", "行きます"],
        },
    ],
    "id": [
        {
            "sentence": "Saya suka kucing dan anjing.",
            "target": "en",
            "probe_tokens": ["suka", "kucing", "anjing"],
        }
    ],
    "en": [
        {
            "sentence": "I like cats and dogs.",
            "target": "id",
            "probe_tokens": ["like", "cats", "dogs"],
        }
    ],
}


async def _download_from_drive(folder_id: str, fname: str, out_dir: Path) -> Path:
    fid = await ensure_file_id(folder_id=folder_id, name=fname, create=False)
    if not fid:
        raise RuntimeError(f"Drive file not found: '{fname}' in folder {folder_id}")
    out_path = out_dir / fname
    out_dir.mkdir(parents=True, exist_ok=True)
    await download_to_path(file_id=fid, out_path=str(out_path))
    if not out_path.exists() or out_path.stat().st_size <= 0:
        raise RuntimeError(f"Download failed or empty: {out_path}")
    return out_path


async def _lookup(store: LocalDictStore, word: str, *, target: str, strict: bool) -> str:
    try:
        got = await store.lookup(word, target_code=target)
    except Exception as e:
        if strict:
            raise
        return f"[ERR] lookup '{word}': {type(e).__name__}: {e}"
    if got and str(got).strip():
        return f"[OK] '{word}' -> {target} = {got}"
    return f"[MISS] '{word}' -> {target} (empty)"


def _whitespace_tokens(sentence: str) -> List[str]:
    toks = [t.strip(".,!?;:()[]{}\"'") for t in sentence.split()]
    return [t for t in toks if t]


async def _sentence_probe(store: LocalDictStore, lang: str, case: Dict[str, object], *, strict: bool) -> Tuple[bool, List[str]]:
    sentence = str(case["sentence"])
    target = str(case.get("target", "en"))
    probe_tokens = [str(x) for x in case.get("probe_tokens", [])]

    # For space-delimited scripts, also probe actual tokens.
    if lang in {"en", "id"}:
        for t in _whitespace_tokens(sentence):
            if t not in probe_tokens:
                probe_tokens.append(t)

    logs: List[str] = []
    hit = False
    logs.append(f"[INFO] sentence({lang}): {sentence}")
    for tok in probe_tokens:
        msg = await _lookup(store, tok, target=target, strict=strict)
        logs.append("  " + msg)
        if msg.startswith("[OK]"):
            hit = True
    return hit, logs


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder-id", default=_env("DICT_GDRIVE_FOLDER_ID", "").strip())
    ap.add_argument("--langs", default="ja", help="Comma-separated: ja,ko,zh,id,en (default: ja)")
    ap.add_argument("--include-en", action="store_true", help="Also run EN word + sentence probes (may be huge depending on your EN file).")
    ap.add_argument("--max-mb", type=int, default=0, help="If >0, abort if any downloaded file exceeds this size (MB). Guard for EN.")
    ap.add_argument("--keep-temp", action="store_true", help="Do not delete temp folder (debug)")
    ap.add_argument("--strict", action="store_true", help="Fail on any exception")
    args = ap.parse_args()

    if not args.folder_id:
        print("[FAIL] DICT_GDRIVE_FOLDER_ID not set and --folder-id not provided.")
        return 2
    if not _have_drive_auth():
        print("[FAIL] Drive auth not available. Set GDRIVE_ACCESS_TOKEN or (GDRIVE_REFRESH_TOKEN + GDRIVE_CLIENT_ID + GDRIVE_CLIENT_SECRET).")
        return 3

    langs = [s.strip() for s in args.langs.split(",") if s.strip()]
    if args.include_en and "en" not in langs:
        langs.append("en")

    files = _dict_files_from_env()

    temp_root = Path(tempfile.mkdtemp(prefix="nixe_gdrive_sentence_smoke_"))
    try:
        print(f"[INFO] temp_dir={temp_root}")
        print(f"[INFO] folder_id={args.folder_id}")
        print(f"[INFO] langs={langs}")

        # Download dict dumps for selected langs
        for lang in langs:
            fname = files.get(lang)
            if not fname:
                raise RuntimeError(f"Missing filename mapping for lang={lang}. Set DICT_MAP_ID_*_FILE.")
            p = await _download_from_drive(args.folder_id, fname, temp_root)
            size_mb = p.stat().st_size / (1024 * 1024)
            if args.max_mb > 0 and size_mb > args.max_mb:
                raise RuntimeError(f"Downloaded file too large ({size_mb:.1f}MB > {args.max_mb}MB): {p.name}")
            print(f"[OK] downloaded {lang}: {p.name} size={int(p.stat().st_size)}")

        # Point dict system to temp dir (offline)
        os.environ["DICT_ENABLE"] = "1"
        os.environ["DICT_DIR"] = str(temp_root)
        os.environ["DICT_GDRIVE_ENABLE"] = "0"

        store = LocalDictStore()
        await store.bootstrap()

        ok_all = True

        # 1) Single word probes
        for lang in langs:
            for w, tgt in WORD_SAMPLES.get(lang, []):
                msg = await _lookup(store, w, target=tgt, strict=args.strict)
                print(f"[WORD] {lang} {msg}")
                if msg.startswith("[MISS]") or msg.startswith("[ERR]"):
                    # don't fail the whole suite on one miss; we'll rely on sentence probes for stricter signal
                    pass

        # 2) Sentence probes (must hit at least one token per sentence)
        for lang in langs:
            cases = SENTENCE_CASES.get(lang, [])
            if not cases:
                continue
            for case in cases:
                hit, logs = await _sentence_probe(store, lang, case, strict=args.strict)
                for line in logs:
                    print(line)
                if not hit:
                    print(f"[FAIL] sentence probe({lang}): no token hits")
                    ok_all = False
                else:
                    print(f"[OK] sentence probe({lang}): at least one token hit")

        if ok_all:
            print("[PASS] gdrive->temp download + word+sentence probes OK.")
            return 0
        return 4
    finally:
        if args.keep_temp:
            print(f"[INFO] keep-temp enabled; leaving {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
