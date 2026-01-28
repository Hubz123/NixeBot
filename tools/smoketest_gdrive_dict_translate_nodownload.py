#!/usr/bin/env python3
"""
NO-DOWNLOAD smoketest for Google Drive dict + offline translate lookup.

Policy:
- MUST NOT download anything.
- Validates Drive auth + folder listing (presence check).
- Validates offline lookup using ONLY local files under DICT_DIR.

Env (secrets, set in Render or locally):
- GDRIVE_ACCESS_TOKEN
  OR
- GDRIVE_REFRESH_TOKEN + GDRIVE_CLIENT_ID + GDRIVE_CLIENT_SECRET

Env (non-secret, may come from runtime_env.json overlay):
- DICT_GDRIVE_FOLDER_ID
- DICT_DIR (default: data/dicts)
- DICT_MAP_ID_{JA,KO,ZH,ID,EN}_FILE (defaults as below)
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from nixe.storage.gdrive import ensure_file_id, find_folder_id_by_name
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



_DEFAULT_SPLIT_FOLDERS = {
    "ja": "JP",
    "ko": "KR",
    "zh": "CN",
    "id": "ID",
    "en": "EN",
}

def _split_folder_for(lang_code: str) -> str:
    code = (lang_code or "").strip().lower()
    return _DEFAULT_SPLIT_FOLDERS.get(code, code.upper() or "EN")

def _has_split_local(dict_dir: Path, lang_code: str) -> bool:
    man = dict_dir / _split_folder_for(lang_code) / "manifest.json"
    return man.exists() and man.stat().st_size > 0

def _dict_files_from_env() -> Dict[str, str]:
    return {
        "ja": _env("DICT_MAP_ID_JA_FILE", "ja-extract.jsonl.gz"),
        "ko": _env("DICT_MAP_ID_KO_FILE", "ko-extract.jsonl.gz"),
        "zh": _env("DICT_MAP_ID_ZH_FILE", "zh-extract.jsonl.gz"),
        "id": _env("DICT_MAP_ID_ID_FILE", "id-extract.jsonl.gz"),
        "en": _env("DICT_MAP_ID_EN_FILE", "raw-wiktextract-data.jsonl.gz"),
    }


async def _check_drive_presence(folder_id: str, name_or_folder: str, *, want_folder: bool) -> Tuple[bool, Optional[str]]:
    """Returns (ok, id). No downloads."""
    if want_folder:
        fid = await find_folder_id_by_name(parent_folder_id=folder_id, name=name_or_folder)
        return (bool(fid), fid)
    fid = await ensure_file_id(folder_id=folder_id, name=name_or_folder, create=False)
    return (bool(fid), fid)

def _check_local_file(dict_dir: Path, fname: str) -> Tuple[bool, Path]:
    p = dict_dir / fname
    if p.exists() and p.stat().st_size > 0:
        return True, p
    return False, p


async def _lookup_with_samples(store: LocalDictStore, lang_code: str, *, strict: bool) -> Tuple[bool, str]:
    samples = LANG_SAMPLES.get(lang_code, [])
    for word, tgt in samples:
        try:
            got = await store.lookup(word, target_code=tgt)
        except Exception as e:
            if strict:
                raise
            got = None
        if got and str(got).strip():
            return True, f"{lang_code}: '{word}' -> {tgt} = {got}"
    return False, f"{lang_code}: no payload returned for samples"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder-id", default=_env("DICT_GDRIVE_FOLDER_ID", "").strip(), help="Google Drive folder id that contains dict dumps")
    ap.add_argument("--dict-dir", default=_env("DICT_DIR", "data/dicts"), help="Local dict directory (same as DICT_DIR)")
    ap.add_argument("--include-en", action="store_true", help="Also validate EN presence and run EN lookup if local EN file exists.")
    ap.add_argument("--strict-local", action="store_true", help="Fail if any required local file is missing.")
    ap.add_argument("--strict", action="store_true", help="Fail on any exception (default is best-effort per sample).")
    args = ap.parse_args()

    if not args.folder_id:
        print("[FAIL] DICT_GDRIVE_FOLDER_ID not set and --folder-id not provided.")
        return 2

    if not _have_drive_auth():
        print("[SKIP] Drive auth not available. Set GDRIVE_ACCESS_TOKEN or (GDRIVE_REFRESH_TOKEN + GDRIVE_CLIENT_ID + GDRIVE_CLIENT_SECRET).")
        return 0

    os.environ.setdefault("DICT_ENABLE", "1")
    os.environ.setdefault("DICT_GDRIVE_ENABLE", "1")
    os.environ.setdefault("DICT_GDRIVE_FOLDER_ID", args.folder_id)
    os.environ.setdefault("DICT_DIR", args.dict_dir)
    os.environ.setdefault("DICT_GDRIVE_NO_DOWNLOAD", "1")

    dict_dir = Path(args.dict_dir)
    files = _dict_files_from_env()

    langs = ["ja", "ko", "zh", "id"]
    if args.include_en:
        langs.append("en")

    print(f"[INFO] folder_id={args.folder_id}")
    print(f"[INFO] dict_dir={dict_dir} (NO-DOWNLOAD) include_en={args.include_en} strict_local={args.strict_local}")

    # 1) Presence in Drive (file OR split-folder layout)
    ok_all = True
    for lang in langs:
        fname = files.get(lang, "")
        ok_file = False
        fid_file: Optional[str] = None
        if fname:
            ok_file, fid_file = await _check_drive_presence(args.folder_id, fname, want_folder=False)
        folder_name = _split_folder_for(lang)
        ok_folder, fid_folder = await _check_drive_presence(args.folder_id, folder_name, want_folder=True)

        if ok_file:
            print(f"[OK] drive presence: {lang} file='{fname}' id={fid_file}")
        elif ok_folder:
            print(f"[OK] drive presence: {lang} folder='{folder_name}' id={fid_folder}")
        else:
            print(f"[FAIL] drive presence: {lang} neither file='{fname}' nor folder='{folder_name}' found")
            ok_all = False

    if not ok_all:
        return 3

    # 2) Local data must exist for offline lookup (monolithic file OR split-folder manifest).
    local_ok = True
    local_paths: Dict[str, Path] = {}
    local_split: Dict[str, bool] = {}
    for lang in langs:
        fname = files.get(lang, "")
        ok_file, p = _check_local_file(dict_dir, fname) if fname else (False, dict_dir / fname)
        ok_split = _has_split_local(dict_dir, lang)
        local_split[lang] = ok_split
        if ok_file:
            local_paths[lang] = p
            print(f"[OK] local file: {lang} {p} size={p.stat().st_size}")
        elif ok_split:
            man = dict_dir / _split_folder_for(lang) / "manifest.json"
            print(f"[OK] local split: {lang} {man}")
        else:
            if lang == "en" and args.include_en and not args.strict_local:
                print(f"[SKIP] local data: en missing (ok) expected file={p} or split={dict_dir / _split_folder_for(lang)}")
                continue
            print(f"[FAIL] local data missing: {lang} expected file={p} or split={dict_dir / _split_folder_for(lang)}")
            local_ok = False

    if not local_ok:
        return 4

    # 3) Offline translate lookup
    store = LocalDictStore()
    await store.bootstrap()

    lookup_ok_all = True
    for lang in langs:
        if lang == "en" and args.include_en and lang not in local_paths:
            print("[SKIP] en: lookup skipped (local EN file not present).")
            continue
        ok, msg = await _lookup_with_samples(store, lang, strict=args.strict)
        if ok:
            print("[OK] lookup:", msg)
        else:
            print("[FAIL] lookup:", msg)
            lookup_ok_all = False

    if not lookup_ok_all:
        return 5

    print("[PASS] NO-DOWNLOAD gdrive presence + offline translate lookup OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
