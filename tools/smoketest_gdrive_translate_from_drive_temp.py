#!/usr/bin/env python3
"""
Smoketest: Translate lookup using dictionary files fetched FROM GOOGLE DRIVE (temp), then cleaned up.

Goal:
- You want a test that proves: Drive auth ok + can download JP dict + offline lookup returns payload.
- Leaves NO persistent dict files in repo (downloads into temp, deletes at end).

Behavior:
- Downloads required language dumps from Drive into a temp folder.
- Boots LocalDictStore pointing DICT_DIR to that temp folder.
- Runs lookup samples (focus JP by default).
- Deletes temp folder unless --keep-temp.

Env (secrets):
- GDRIVE_ACCESS_TOKEN
  OR
- GDRIVE_REFRESH_TOKEN + GDRIVE_CLIENT_ID + GDRIVE_CLIENT_SECRET

Env / args (non-secret):
- DICT_GDRIVE_FOLDER_ID (or --folder-id)
- DICT_MAP_ID_JA_FILE etc (or defaults)
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

# Make imports robust even if user doesn't set PYTHONPATH=.
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


LANG_SAMPLES: Dict[str, List[Tuple[str, str]]] = {
    "ja": [("猫", "en"), ("犬", "en"), ("ありがとう", "en")],
    "ko": [("사랑", "en"), ("고양이", "en"), ("안녕하세요", "en")],
    "zh": [("你好", "en"), ("猫", "en"), ("谢谢", "en")],
    "id": [("kucing", "en"), ("anjing", "en"), ("terima kasih", "en")],
    "en": [("cat", "id"), ("dog", "id"), ("hello", "id")],
}


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


async def _lookup(store: LocalDictStore, lang: str, *, strict: bool) -> str:
    for word, tgt in LANG_SAMPLES.get(lang, []):
        try:
            got = await store.lookup(word, target_code=tgt)
        except Exception:
            if strict:
                raise
            continue
        if got and str(got).strip():
            return f"[OK] {lang}: '{word}' -> {tgt} = {got}"
    return f"[FAIL] {lang}: no payload returned for samples"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder-id", default=_env("DICT_GDRIVE_FOLDER_ID", "").strip())
    ap.add_argument("--langs", default="ja", help="Comma-separated: ja,ko,zh,id,en (default: ja)")
    ap.add_argument("--keep-temp", action="store_true", help="Do not delete temp download folder")
    ap.add_argument("--strict", action="store_true", help="Fail on any exception")
    ap.add_argument("--include-en", action="store_true", help="Also test EN (WARNING: may be huge if your EN file is huge)")
    ap.add_argument("--max-mb", type=int, default=0, help="If >0, abort if any downloaded file exceeds this size (MB). Useful to guard EN.")
    args = ap.parse_args()

    if not args.folder_id:
        print("[FAIL] DICT_GDRIVE_FOLDER_ID not set and --folder-id not provided.")
        return 2

    if not _have_drive_auth():
        print("[SKIP] Drive auth not available. Set GDRIVE_ACCESS_TOKEN or (GDRIVE_REFRESH_TOKEN + GDRIVE_CLIENT_ID + GDRIVE_CLIENT_SECRET).")
        return 0

    langs = [s.strip() for s in args.langs.split(",") if s.strip()]
    if args.include_en and "en" not in langs:
        langs.append("en")

    files = _dict_files_from_env()

    temp_root = Path(tempfile.mkdtemp(prefix="nixe_gdrive_dict_smoke_"))
    try:
        print(f"[INFO] temp_dir={temp_root}")
        print(f"[INFO] folder_id={args.folder_id}")
        print(f"[INFO] langs={langs}")

        # Download required files
        for lang in langs:
            fname = files.get(lang)
            if not fname:
                raise RuntimeError(f"Missing filename mapping for lang={lang}. Set DICT_MAP_ID_*_FILE.")
            p = await _download_from_drive(args.folder_id, fname, temp_root)
            size_mb = p.stat().st_size / (1024 * 1024)
            if args.max_mb > 0 and size_mb > args.max_mb:
                raise RuntimeError(f"Downloaded file too large ({size_mb:.1f}MB > {args.max_mb}MB): {p.name}")
            print(f"[OK] downloaded {lang}: {p.name} size={int(p.stat().st_size)}")

        # Point dict system to temp dir
        os.environ["DICT_ENABLE"] = "1"
        os.environ["DICT_DIR"] = str(temp_root)
        os.environ["DICT_GDRIVE_ENABLE"] = "0"  # offline lookup now uses local temp files

        store = LocalDictStore()
        await store.bootstrap()

        ok_all = True
        for lang in langs:
            msg = await _lookup(store, lang, strict=args.strict)
            print(msg)
            if msg.startswith("[FAIL]"):
                ok_all = False

        if ok_all:
            print("[PASS] gdrive->temp download + offline lookup OK.")
            return 0
        return 4
    finally:
        if args.keep_temp:
            print(f"[INFO] keep-temp enabled; leaving {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
