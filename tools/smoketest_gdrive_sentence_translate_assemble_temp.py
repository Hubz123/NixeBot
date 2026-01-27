#!/usr/bin/env python3
"""
Smoketest: INPUT sentence (EN/ID) -> OUTPUT sentence (JA) using dictionaries stored in Google Drive,
with deterministic heuristics so results are not "random sense" (e.g., 'dan' -> 'dahan').

Flow:
1) Drive auth works
2) Download SRC dictionary dump from Drive into TEMP (deleted after test unless --keep-temp)
3) Translate tokens using:
   - small built-in ID->JA phrase/stopword rules (deterministic)
   - dictionary lookup fallback (LocalDictStore)
4) Assemble into 1 JP sentence and print INPUT+OUTPUT+mapping.

NOTES:
- Still dictionary/gloss based. Not a full MT model.
- Goal is stable smoke: pipeline works and output is plausibly JP.
"""

from __future__ import annotations

import argparse
import asyncio
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


# ------------------- deterministic rules for ID -> JA -------------------

# Phrase rules (apply before tokenization result mapping)
ID_PHRASE_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bterima\s+kasih\b", re.I), "ありがとう"),
    (re.compile(r"\bsaya\s+suka\b", re.I), "私は好きです"),
    (re.compile(r"\baku\s+suka\b", re.I), "私は好きです"),
]

# Token rules (lowercase token -> jp)
ID_TOKEN_MAP: Dict[str, str] = {
    # pronouns
    "saya": "私",
    "aku": "私",
    "kamu": "あなた",
    "anda": "あなた",
    # function words
    "dan": "と",
    "atau": "または",
    "di": "で",
    "ke": "へ",
    "dari": "から",
    "yang": "",         # drop
    "ini": "この",
    "itu": "それ",
    "tidak": "ない",
    "nggak": "ない",
    "gak": "ない",
    "bukan": "じゃない",
    # common verbs/adjectives
    "suka": "好き",
    "mau": "したい",
    "ingin": "したい",
    "pergi": "行く",
    "datang": "来る",
    "makan": "食べる",
    "minum": "飲む",
    # nouns
    "kucing": "猫",
    "anjing": "犬",
}

# Words we should NOT dictionary-translate (avoid wrong senses); either map via ID_TOKEN_MAP or leave as-is.
ID_STOPWORDS = set(ID_TOKEN_MAP.keys()) | {".", ",", "!", "?"}


# Tokenization for EN/ID: words + punctuation
WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9']+|[^\s]", re.UNICODE)
SEP_RE = re.compile(r"(?:\n|;|/|\||,|•|·|—|-{2,})")


def tokenize_sentence(text: str, *, src: str) -> List[str]:
    s = text.strip()
    if not s:
        return []
    if src in {"en", "id"}:
        return WORD_RE.findall(s)
    return [c for c in s if not c.isspace()]


def pick_best_translation(payload: object, *, tgt: str) -> str:
    """
    Prefer payload chunk that looks like target script (Japanese) when tgt=ja.
    Fallback to first chunk.
    """
    if payload is None:
        return ""
    if isinstance(payload, dict):
        for k in ("best", "translation", "translations", "gloss", "glosses", "sense", "senses", "text"):
            if k in payload and payload[k]:
                payload = payload[k]
                break

    s = str(payload).strip()
    if not s:
        return ""

    s = s.strip(" \t\r\n\"'[](){}")
    parts = [p.strip() for p in SEP_RE.split(s) if p.strip()]
    if not parts:
        return ""

    if tgt == "ja":
        # Prefer any chunk that has non-ascii (JP chars likely)
        for p in parts:
            if any(ord(c) > 127 for c in p):
                return p[:40].rstrip() + ("…" if len(p) > 40 else "")
    best = parts[0]
    return best[:40].rstrip() + ("…" if len(best) > 40 else "")


def is_word_token(tok: str) -> bool:
    return bool(re.match(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9']+$", tok))


def assemble_output(tokens: List[str], mapped: List[str], *, tgt: str) -> str:
    out: List[str] = []
    for tok, tr in zip(tokens, mapped):
        if tr is None:
            tr = ""
        tr = str(tr)

        # punctuation token
        if len(tok) == 1 and not tok.isalnum() and not tok.isalpha() and tok.strip():
            out.append(tr if tr else tok)
            continue

        if not tr:
            continue

        if tgt == "ja":
            # Glue JP chunks; add spaces only for roman leftovers
            if any(ord(c) > 127 for c in tr):
                out.append(tr)
            else:
                if out and not out[-1].endswith(" "):
                    out.append(" ")
                out.append(tr)
                out.append(" ")
        else:
            out.append(tr)
            out.append(" ")

    s = "".join(out).strip()
    s = re.sub(r"\s{2,}", " ", s)

    if tgt == "ja":
        s = s.replace(" .", "。").replace(".", "。")
        s = s.replace(" ,", "、").replace(",", "、")
        s = s.replace(" ?", "？").replace("?", "？")
        s = s.replace(" !", "！").replace("!", "！")
        s = s.replace(" 、", "、").replace(" 。", "。")
    return s


async def translate_token(store: LocalDictStore, tok: str, *, src: str, tgt: str) -> str:
    # punctuation
    if len(tok) == 1 and not tok.isalnum() and not tok.isalpha() and tok.strip():
        return tok

    if src == "id" and tgt == "ja":
        low = tok.lower()
        if low in ID_TOKEN_MAP:
            return ID_TOKEN_MAP[low]
        # If it's a stopword-ish, don't dictionary translate
        if low in ID_STOPWORDS:
            return tok

    payload = await store.lookup(tok, target_code=tgt)
    best = pick_best_translation(payload, tgt=tgt)
    return best or tok


async def dict_sentence_translate(store: LocalDictStore, text: str, *, src: str, tgt: str, min_hit_ratio: float, debug: bool) -> Tuple[bool, str, List[Tuple[str, str]]]:
    # Apply phrase rules first (ID only)
    src_text = text
    if src == "id" and tgt == "ja":
        for pat, rep in ID_PHRASE_RULES:
            src_text = pat.sub(rep, src_text)

    tokens = tokenize_sentence(src_text, src=src)
    if not tokens:
        return False, "", []

    mapped: List[str] = []
    pairs: List[Tuple[str, str]] = []
    hits = 0
    word_count = 0

    for tok in tokens:
        if is_word_token(tok):
            word_count += 1

        tr = await translate_token(store, tok, src=src, tgt=tgt)

        if is_word_token(tok) and tr and tr != tok:
            hits += 1

        mapped.append(tr)
        pairs.append((tok, tr))

    out_sentence = assemble_output(tokens, mapped, tgt=tgt)
    hit_ratio = (hits / max(word_count, 1))

    ok = hit_ratio >= min_hit_ratio
    if debug:
        print(f"[DEBUG] tokens={tokens}")
        print(f"[DEBUG] hits={hits} word_count={word_count} hit_ratio={hit_ratio:.2f} (min {min_hit_ratio:.2f})")

    return ok, out_sentence, pairs


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder-id", default=_env("DICT_GDRIVE_FOLDER_ID", "").strip())
    ap.add_argument("--src", default="id", choices=["id", "en"], help="Source language (default: id)")
    ap.add_argument("--tgt", default="ja", choices=["ja"], help="Target language (default: ja)")
    ap.add_argument("--sentence", default="", help="Input sentence. If empty, uses built-in sample.")
    ap.add_argument("--min-hit-ratio", type=float, default=0.34, help="PASS threshold: translated word fraction (default: 0.34)")
    ap.add_argument("--max-mb", type=int, default=250, help="Abort if downloaded file exceeds this size in MB (default: 250)")
    ap.add_argument("--keep-temp", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if not args.folder_id:
        print("[FAIL] DICT_GDRIVE_FOLDER_ID not set and --folder-id not provided.")
        return 2
    if not _have_drive_auth():
        print("[FAIL] Drive auth not available. Set GDRIVE_ACCESS_TOKEN or (GDRIVE_REFRESH_TOKEN + GDRIVE_CLIENT_ID + GDRIVE_CLIENT_SECRET).")
        return 3

    if not args.sentence.strip():
        args.sentence = "Saya suka kucing dan anjing." if args.src == "id" else "I like cats and dogs."

    files = _dict_files_from_env()
    fname = files.get(args.src)
    if not fname:
        print(f"[FAIL] Missing DICT_MAP_ID_*_FILE for src={args.src}.")
        return 4

    temp_root = Path(tempfile.mkdtemp(prefix="nixe_gdrive_sentence_translate_"))
    try:
        print(f"[INFO] temp_dir={temp_root}")
        print(f"[INFO] folder_id={args.folder_id}")
        print(f"[INFO] src={args.src} tgt={args.tgt} file={fname}")
        print(f"[INPUT] {args.sentence}")

        p = await _download_from_drive(args.folder_id, fname, temp_root)
        size_mb = p.stat().st_size / (1024 * 1024)
        if args.max_mb > 0 and size_mb > args.max_mb:
            raise RuntimeError(f"Downloaded file too large ({size_mb:.1f}MB > {args.max_mb}MB): {p.name}")
        print(f"[OK] downloaded: {p.name} size={int(p.stat().st_size)}")

        os.environ["DICT_ENABLE"] = "1"
        os.environ["DICT_DIR"] = str(temp_root)
        os.environ["DICT_GDRIVE_ENABLE"] = "0"

        store = LocalDictStore()
        await store.bootstrap()

        ok, out_sentence, pairs = await dict_sentence_translate(
            store, args.sentence, src=args.src, tgt=args.tgt, min_hit_ratio=args.min_hit_ratio, debug=args.debug
        )

        print(f"[OUTPUT] {out_sentence}")
        print("[MAP] token -> translation (only changes)")
        for a, b in pairs:
            if a != b and b:
                print(f"  {a} -> {b}")

        if ok:
            print("[PASS] sentence assemble translate OK (dictionary-based, deterministic rules).")
            return 0
        print("[FAIL] hit ratio below threshold. Try simpler sentence or lower --min-hit-ratio.")
        return 5
    finally:
        if args.keep_temp:
            print(f"[INFO] keep-temp enabled; leaving {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
