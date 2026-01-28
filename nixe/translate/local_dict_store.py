"""nixe.translate.local_dict_store

Offline-first short-input lookup using Kaikki/Wiktextract JSONL(.gz) dumps.

This is intentionally *lightweight* (no huge in-memory load):
- Keeps a small on-disk cache (sqlite) per source language file.
- On cache miss, scans the JSONL(.gz) sequentially until it finds a matching "word".
  This is slower for the first lookup, but fast afterward due to caching.

Files expected (configurable):
  DICT_DIR=data/dicts
  DICT_MAP_ID_JA_FILE=ja-extract.jsonl.gz
  DICT_MAP_ID_KO_FILE=ko-extract.jsonl.gz
  DICT_MAP_ID_ZH_FILE=zh-extract.jsonl.gz
  DICT_MAP_ID_ID_FILE=id-extract.jsonl.gz
  DICT_MAP_ID_EN_FILE=raw-wiktextract-data.jsonl.gz  (or en-extract.jsonl.gz)

Google Drive bootstrap (optional):
  DICT_GDRIVE_ENABLE=1
  DICT_GDRIVE_FOLDER_ID=<folder id>
  DICT_GDRIVE_AUTOCREATE=0/1

Drive auth (secrets in Render env):
  - GDRIVE_ACCESS_TOKEN (optional)
  - or refresh flow:
      GDRIVE_REFRESH_TOKEN
      GDRIVE_CLIENT_ID
      GDRIVE_CLIENT_SECRET
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from nixe.storage.gdrive import ensure_file_id, download_to_path, find_folder_id_by_name, download_folder_recursive

log = logging.getLogger(__name__)


def _env(k: str, default: str = "") -> str:
    v = os.getenv(k)
    if v is None:
        return default
    return str(v)


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default



def _gdrive_split_only() -> bool:
    """If true, skip monolithic file probing and treat Drive as split-folder only."""
    return _as_bool(_env("DICT_GDRIVE_SPLIT_ONLY", "0"), False)


def _gdrive_refresh_enabled() -> bool:
    """If true, compare remote manifest.json metadata and refresh local split folder when it changes."""
    return _as_bool(_env("DICT_GDRIVE_REFRESH", "0"), False)


def is_short_input(text: str) -> bool:
    """Heuristic gate: only do offline lookup for short inputs."""
    t = (text or "").strip()
    if not t:
        return False
    max_words = int(float(_env("DICT_SHORT_MAX_WORDS", "3") or 3))
    max_chars = int(float(_env("DICT_SHORT_MAX_CHARS", "48") or 48))
    if len(t) > max_chars:
        return False
    # Count words (split on whitespace)
    words = [w for w in re.split(r"\s+", t) if w]
    if len(words) > max_words:
        return False
    return True


def _detect_source_lang(text: str) -> str:
    """Very small heuristic to select which dump to scan first."""
    t = text or ""
    # Japanese (Hiragana/Katakana/Kanji)
    if re.search(r"[\u3040-\u30ff]", t):
        return "ja"
    # Hangul
    if re.search(r"[\uac00-\ud7af]", t):
        return "ko"
    # CJK (Han) - could be zh/ja; if no kana, treat as zh first
    if re.search(r"[\u4e00-\u9fff]", t):
        return "zh"
    # Default latin -> try id then en
    return "id"


@dataclass(frozen=True)
class DictFiles:
    ja: str
    ko: str
    zh: str
    id: str
    en: str


# Split-layout folder mapping (optional env overrides)
# Default folder names follow the user's Kaikki one-click output: EN/ID/JP/KR/CN
_DEFAULT_SPLIT_FOLDERS = {
    "ja": "JP",
    "ko": "KR",
    "zh": "CN",
    "id": "ID",
    "en": "EN",
}



def _gdrive_langs_allowed() -> Optional[set[str]]:
    """Optional filter for GDrive bootstrap.

    If DICT_GDRIVE_LANGS is set to a comma-separated list (e.g. "id,en"),
    bootstrap will only attempt to download monolithic/split assets for those language codes.
    """
    raw = _env("DICT_GDRIVE_LANGS", "").strip()
    if not raw:
        return None
    out: set[str] = set()
    for t in raw.split(","):
        t = (t or "").strip().lower()
        if t:
            out.add(t)
    return out or None
def _split_folder_for(lang_code: str) -> str:
    code = (lang_code or "").strip().lower()
    if code == "ja":
        return _env("DICT_MAP_ID_JA_FOLDER", _DEFAULT_SPLIT_FOLDERS["ja"])
    if code == "ko":
        return _env("DICT_MAP_ID_KO_FOLDER", _DEFAULT_SPLIT_FOLDERS["ko"])
    if code == "zh":
        return _env("DICT_MAP_ID_ZH_FOLDER", _DEFAULT_SPLIT_FOLDERS["zh"])
    if code == "id":
        return _env("DICT_MAP_ID_ID_FOLDER", _DEFAULT_SPLIT_FOLDERS["id"])
    if code == "en":
        return _env("DICT_MAP_ID_EN_FOLDER", _DEFAULT_SPLIT_FOLDERS["en"])
    return _DEFAULT_SPLIT_FOLDERS.get(code, code.upper() or "EN")

class LocalDictStore:
    """Offline dictionary store with optional GDrive bootstrap."""

    def __init__(self) -> None:
        self.enabled = _as_bool(_env("DICT_ENABLE", "0"), False)
        self.dict_dir = Path(_env("DICT_DIR", "data/dicts"))
        self.files = DictFiles(
            ja=_env("DICT_MAP_ID_JA_FILE", "ja-extract.jsonl.gz"),
            ko=_env("DICT_MAP_ID_KO_FILE", "ko-extract.jsonl.gz"),
            zh=_env("DICT_MAP_ID_ZH_FILE", "zh-extract.jsonl.gz"),
            id=_env("DICT_MAP_ID_ID_FILE", "id-extract.jsonl.gz"),
            en=_env("DICT_MAP_ID_EN_FILE", "raw-wiktextract-data.jsonl.gz"),
        )

        self._lock = asyncio.Lock()
        self._bootstrapped = False

    async def bootstrap(self) -> None:
        """Ensure local dict files exist (optional Drive download). Safe to call repeatedly."""
        if not self.enabled:
            return
        async with self._lock:
            if self._bootstrapped:
                return
            self.dict_dir.mkdir(parents=True, exist_ok=True)

            if _as_bool(_env("DICT_GDRIVE_ENABLE", "0"), False):
                folder_id = _env("DICT_GDRIVE_FOLDER_ID", "").strip()
                if folder_id:
                    no_download = _as_bool(_env("DICT_GDRIVE_NO_DOWNLOAD", "0"), False)
                    if no_download:
                        # e.g. tools/smoketest_gdrive_dict_translate_nodownload.py wants presence checks only.
                        self._bootstrapped = True
                        return
                    # Download required files if missing
                    create = _as_bool(_env("DICT_GDRIVE_AUTOCREATE", "0"), False)
                    allowed_langs = _gdrive_langs_allowed()
                    split_only = _gdrive_split_only()
                    refresh_split = _gdrive_refresh_enabled()
                    if not split_only:
                        for code, fname in [("ja", self.files.ja), ("ko", self.files.ko), ("zh", self.files.zh), ("id", self.files.id), ("en", self.files.en)]:
                            if not fname:
                                continue
                            if allowed_langs is not None and code not in allowed_langs:
                                continue
                            out = self.dict_dir / fname
                            if out.exists() and out.stat().st_size > 0:
                                continue
                            try:
                                file_id = await ensure_file_id(folder_id=folder_id, name=fname, create=create)
                                if file_id:
                                    await download_to_path(file_id=file_id, out_path=str(out))
                            except Exception as e:
                                log.warning("[dict] gdrive download failed for %s: %r", fname, e)
                    else:
                        log.info("[dict] DICT_GDRIVE_SPLIT_ONLY=1; skip monolithic file probes")
# If the configured files are not present in Drive, support split-folder layout (EN/ID/JP/KR/CN).
                    # This keeps runtime_env compatible: we only fallback to folders when files are missing.
                    try:
                        split_codes = ["ja", "ko", "zh", "id", "en"]
                        if allowed_langs is not None:
                            split_codes = [c for c in split_codes if c in allowed_langs]
                        for code in split_codes:
                            folder_name = _split_folder_for(code)
                            local_dir = (self.dict_dir / folder_name)
                            local_manifest = (local_dir / "manifest.json")
                            local_meta = (local_dir / ".remote_manifest_meta.txt")
                    
                            if local_manifest.exists() and local_manifest.stat().st_size > 0 and not refresh_split:
                                continue
                    
                            # Check remote folder exists
                            try:
                                sub_id = await find_folder_id_by_name(parent_folder_id=folder_id, name=folder_name)
                            except Exception:
                                sub_id = None
                            if not sub_id:
                                continue
                    
                            # If refresh enabled, compare remote manifest meta to skip unnecessary downloads
                            marker = ""
                            if refresh_split and local_manifest.exists() and local_manifest.stat().st_size > 0:
                                try:
                                    from nixe.storage.gdrive import find_file_id_by_name, fetch_meta
                                    mfid = await find_file_id_by_name(folder_id=sub_id, name="manifest.json")
                                    if mfid:
                                        meta = await fetch_meta(mfid)
                                        marker = f"{meta.get('modifiedTime','')}|{meta.get('size','')}"
                                        if local_meta.exists() and local_meta.read_text(encoding="utf-8", errors="ignore").strip() == marker:
                                            continue
                                except Exception as e:
                                    log.debug("[dict] refresh compare failed for %s: %r", folder_name, e)
                    
                            # Download (fresh or missing)
                            try:
                                if local_dir.exists() and refresh_split:
                                    # Ensure stale files don't remain
                                    import shutil as _shutil
                                    _shutil.rmtree(str(local_dir), ignore_errors=True)
                                await download_folder_recursive(folder_id=sub_id, out_dir=str(local_dir))
                                log.info("[dict] downloaded split folder %s -> %s", folder_name, str(local_dir))
                                if refresh_split:
                                    # Best-effort write marker
                                    if not marker and local_manifest.exists():
                                        marker = f"{local_manifest.stat().st_mtime}|{local_manifest.stat().st_size}"
                                    try:
                                        local_dir.mkdir(parents=True, exist_ok=True)
                                        local_meta.write_text(marker, encoding="utf-8")
                                    except Exception:
                                        pass
                            except Exception as e:
                                log.warning("[dict] gdrive folder download failed for %s: %r", folder_name, e)
                    except Exception as e:
                        log.debug("[dict] split-folder bootstrap skipped: %r", e)
            self._bootstrapped = True

    def _cache_db_path(self, lang_code: str, filename: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", filename)
        return self.dict_dir / f".cache_{lang_code}_{safe}.sqlite3"

    def _ensure_cache_db(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db_path))
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS cache (word TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            con.commit()
        finally:
            con.close()

    def _cache_get(self, db_path: Path, word: str) -> Optional[str]:
        try:
            con = sqlite3.connect(str(db_path))
            try:
                cur = con.execute("SELECT payload FROM cache WHERE word = ?", (word,))
                row = cur.fetchone()
                return row[0] if row else None
            finally:
                con.close()
        except Exception:
            return None

    def _cache_put(self, db_path: Path, word: str, payload: str) -> None:
        try:
            con = sqlite3.connect(str(db_path))
            try:
                con.execute(
                    "INSERT OR REPLACE INTO cache(word, payload) VALUES(?,?)",
                    (word, payload),
                )
                con.commit()
            finally:
                con.close()
        except Exception:
            pass

    def _iter_json_lines(self, path: Path) -> Iterable[Dict[str, Any]]:
        if not path.exists():
            return
        if path.name.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            yield obj
                    except Exception:
                        continue
        else:
            with path.open("rt", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            yield obj
                    except Exception:
                        continue

# -------------------------
    # Split-layout support (LangNixe: EN/ID/JP/KR/CN with manifest.json + part files)
    # -------------------------

    def _split_dir(self, src_code: str) -> Path:
        return self.dict_dir / _split_folder_for(src_code)

    def _split_manifest(self, src_code: str) -> Optional[Dict[str, Any]]:
        man = self._split_dir(src_code) / "manifest.json"
        if not man.exists():
            return None
        try:
            return json.loads(man.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            return None

    def _split_index_db_path(self, src_code: str) -> Path:
        folder = _split_folder_for(src_code)
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", folder)
        return self.dict_dir / f".split_idx_{safe}.sqlite3"

    def _ensure_split_index(self, src_code: str, manifest: Dict[str, Any]) -> Path:
        db_path = self._split_index_db_path(src_code)
        if db_path.exists() and db_path.stat().st_size > 0:
            return db_path

        # Build a lightweight index: word -> relfile
        self._ensure_cache_db(db_path)  # creates 'cache' table; we'll create our own table too.
        con = sqlite3.connect(str(db_path))
        try:
            con.execute("CREATE TABLE IF NOT EXISTS idx (word TEXT PRIMARY KEY, relfile TEXT NOT NULL)")
            con.commit()

            out_files = manifest.get("output_files") or []
            if not isinstance(out_files, list):
                out_files = []

            split_base = self._split_dir(src_code)

            inserted = 0
            for of in out_files:
                if not isinstance(of, dict):
                    continue
                rel = of.get("file")
                if not isinstance(rel, str) or not rel:
                    continue
                p = split_base / rel
                if not p.exists():
                    continue
                # Scan this part and index words
                for obj in self._iter_json_lines(p):
                    w = obj.get("word") or obj.get("title") or obj.get("term")
                    if not isinstance(w, str) or not w:
                        continue
                    try:
                        con.execute("INSERT OR IGNORE INTO idx(word, relfile) VALUES(?,?)", (w, rel))
                        inserted += 1
                        if inserted % 5000 == 0:
                            con.commit()
                    except Exception:
                        continue
            con.commit()
        finally:
            con.close()
        return db_path

    def _lookup_split_sync(self, word: str, src_code: str, target_code: str) -> Optional[str]:
        manifest = self._split_manifest(src_code)
        if not manifest:
            return None

        db_path = self._ensure_split_index(src_code, manifest)
        relfile: Optional[str] = None
        try:
            con = sqlite3.connect(str(db_path))
            try:
                cur = con.execute("SELECT relfile FROM idx WHERE word = ?", (word,))
                row = cur.fetchone()
                relfile = row[0] if row else None
            finally:
                con.close()
        except Exception:
            relfile = None

        if not relfile:
            return None

        p = self._split_dir(src_code) / relfile
        found: Optional[Dict[str, Any]] = None
        for obj in self._iter_json_lines(p):
            w = obj.get("word") or obj.get("title") or obj.get("term")
            if isinstance(w, str) and w == word:
                found = obj
                break
        if not found:
            return None

        trans = self._extract_translations(found, target_code=target_code)
        gloss = self._extract_glosses(found)

        if trans:
            return ", ".join(trans[:8])
        if gloss:
            return "; ".join(gloss[:3])
        return None

    def lookup_term_sync(self, term: str, target_code: str) -> Optional[str]:
        """Lookup a single term (no short-input gating)."""
        if not self.enabled:
            return None
        word = (term or "").strip()
        if not word:
            return None

        src_first = _detect_source_lang(word)
        candidates: List[Tuple[str, str]] = []
        if src_first == "ja":
            candidates = [("ja", self.files.ja), ("en", self.files.en)]
        elif src_first == "ko":
            candidates = [("ko", self.files.ko), ("en", self.files.en)]
        elif src_first == "zh":
            candidates = [("zh", self.files.zh), ("en", self.files.en)]
        else:
            candidates = [("id", self.files.id), ("en", self.files.en)]

        for src_code, fname in candidates:
            # Prefer split layout if available
            man = self._split_dir(src_code) / "manifest.json"
            if man.exists():
                payload = self._lookup_split_sync(word, src_code=src_code, target_code=target_code)
                if payload:
                    return payload

            # Fallback to monolithic file
            if not fname:
                continue
            fpath = self.dict_dir / fname
            db_path = self._cache_db_path(src_code, fname)
            self._ensure_cache_db(db_path)

            cached = self._cache_get(db_path, word)
            if cached:
                return cached

            found: Optional[Dict[str, Any]] = None
            for obj in self._iter_json_lines(fpath):
                w = obj.get("word") or obj.get("title") or obj.get("term")
                if isinstance(w, str) and w == word:
                    found = obj
                    break

            if not found:
                continue

            trans = self._extract_translations(found, target_code=target_code)
            gloss = self._extract_glosses(found)

            if trans:
                payload = ", ".join(trans[:8])
            elif gloss:
                payload = "; ".join(gloss[:3])
            else:
                payload = ""

            if payload:
                self._cache_put(db_path, word, payload)
                return payload

        return None

    async def build_glossary_block(self, text: str, target_code: str) -> str:
        """Build a compact glossary block from the local dictionaries for use in MT prompts."""
        if not self.enabled or not _as_bool(_env("DICT_GLOSSARY_ENABLE", "0"), False):
            return ""
        await self.bootstrap()

        try:
            max_terms = int(float(_env("DICT_GLOSSARY_MAX_TERMS", "12")))
        except Exception:
            max_terms = 12
        try:
            max_chars = int(float(_env("DICT_GLOSSARY_MAX_CHARS", "800")))
        except Exception:
            max_chars = 800
        if max_terms <= 0 or max_chars <= 0:
            return ""

        # Candidate extraction: words + CJK runs
        cands = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9']{2,}|[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]{1,}", text or "")
        # Prefer longer tokens first, de-dupe
        uniq = []
        seen = set()
        for w in sorted(cands, key=lambda s: (-len(s), s)):
            if w in seen:
                continue
            seen.add(w)
            # avoid super-long junk
            if len(w) > 40:
                continue
            uniq.append(w)
            if len(uniq) >= max_terms * 4:
                break

        pairs: list[tuple[str, str]] = []
        loop = asyncio.get_running_loop()
        for w in uniq:
            if len(pairs) >= max_terms:
                break
            # run sync lookup in executor (it can scan gzip)
            hit = await loop.run_in_executor(None, self.lookup_term_sync, w, target_code)
            if hit:
                pairs.append((w, hit))

        if not pairs:
            return ""

        lines = ["GLOSSARY (MUST FOLLOW WHEN APPLICABLE; prefer exact term translations):"]
        for a, b in pairs[:max_terms]:
            lines.append(f"- {a} => {b}")
        out = "\n".join(lines)
        return out[:max_chars]

    def _extract_translations(self, obj: Dict[str, Any], target_code: str) -> List[str]:
        out: List[str] = []
        translations = obj.get("translations") or obj.get("translation") or []
        if isinstance(translations, dict):
            translations = [translations]
        if isinstance(translations, list):
            for t in translations:
                if not isinstance(t, dict):
                    continue
                # common keys in wiktextract dumps
                lang_code = (t.get("lang_code") or t.get("lang") or t.get("code") or "").lower()
                if lang_code and lang_code != target_code.lower():
                    continue
                w = t.get("word") or t.get("term") or t.get("translation")
                if isinstance(w, str) and w.strip():
                    out.append(w.strip())
        # de-dup preserve order
        seen=set()
        uniq=[]
        for w in out:
            if w not in seen:
                seen.add(w); uniq.append(w)
        return uniq

    def _extract_glosses(self, obj: Dict[str, Any]) -> List[str]:
        senses = obj.get("senses") or []
        glosses: List[str] = []
        if isinstance(senses, dict):
            senses = [senses]
        if isinstance(senses, list):
            for s in senses:
                if not isinstance(s, dict):
                    continue
                g = s.get("glosses") or s.get("gloss") or s.get("sense") or s.get("definition")
                if isinstance(g, str) and g.strip():
                    glosses.append(g.strip())
                elif isinstance(g, list):
                    for gg in g:
                        if isinstance(gg, str) and gg.strip():
                            glosses.append(gg.strip())
        seen=set()
        uniq=[]
        for g in glosses:
            if g not in seen:
                seen.add(g); uniq.append(g)
        return uniq[:5]

    def lookup_sync(self, text: str, target_code: str) -> Optional[str]:
        """Synchronous lookup (call in executor for safety)."""
        if not self.enabled or not is_short_input(text):
            return None
        word = (text or "").strip()
        if not word:
            return None
        return self.lookup_term_sync(word, target_code=target_code)

    async def lookup(self, text: str, target_code: str) -> Optional[str]:
        """Async wrapper."""
        await self.bootstrap()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.lookup_sync, text, target_code)


    async def sentence_translate_assemble(self, text: str, target_code: str = "ja", source_code: Optional[str] = None) -> Optional[str]:
        """
        Dictionary-based sentence translation (gloss-style) with deterministic rules.

        Returns:
          - assembled output string, or None if disabled / unsupported.

        Supported:
          - source: id/en (auto-detect if None)
          - target: ja
        """
        await self.bootstrap()
        if not self.enabled:
            return None
        tcode = (target_code or "").strip().lower()
        if tcode != "ja":
            return None
        if not text or not str(text).strip():
            return None
        # Avoid doing this for already-CJK inputs
        if _has_cjk(text):
            return None

        src = (source_code or "").strip().lower() if source_code else _detect_src_sentence(text)
        if src not in {"id", "en"}:
            return None

        assembler = _SentenceAssembler(self)

        loop = asyncio.get_running_loop()
        if src == "id":
            out, _pairs = await loop.run_in_executor(None, assembler.translate_id_to_ja, text)
        else:
            out, _pairs = await loop.run_in_executor(None, assembler.translate_en_to_ja, text)

        return (out or "").strip() or None


# ---------------------------------------------------------------------------
# Sentence assembly (dictionary-based "gloss" translator)
# ---------------------------------------------------------------------------

_EN_ID_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9']+|[^\s]", re.UNICODE)
_SPLIT_SEP_RE = re.compile(r"(?:\n|;|/|\||,|•|·|—|-{2,})")

# Deterministic rules to avoid wrong senses (ID/EN -> JA)
_ID_TOKEN_MAP: Dict[str, str] = {
    # pronouns
    "saya": "私",
    "aku": "私",
    "kamu": "あなた",
    "anda": "あなた",
    # conjunctions / particles
    "dan": "と",
    "atau": "または",
    # prepositions (rough defaults)
    "di": "で",
    "ke": "へ",
    "dari": "から",
    # misc
    "yang": "",  # drop
    "ini": "この",
    "itu": "それ",
    "tidak": "ない",
    "nggak": "ない",
    "gak": "ない",
    "bukan": "じゃない",
    # common
    "suka": "好き",
    "mau": "したい",
    "ingin": "したい",
    "pergi": "行く",
    "datang": "来る",
    "makan": "食べる",
    "minum": "飲む",
    # demo nouns (extend as needed)
    "kucing": "猫",
    "anjing": "犬",
}

_EN_TOKEN_MAP: Dict[str, str] = {
    "i": "私",
    "we": "私たち",
    "you": "あなた",
    "like": "好き",
    "and": "と",
    "or": "または",
    "cat": "猫",
    "cats": "猫",
    "dog": "犬",
    "dogs": "犬",
}

_ID_STOPWORDS = set(_ID_TOKEN_MAP.keys())
_EN_STOPWORDS = set(_EN_TOKEN_MAP.keys())


def _has_cjk(text: str) -> bool:
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:  # hiragana/katakana
            return True
        if 0x4E00 <= o <= 0x9FFF:  # CJK unified
            return True
        if 0xAC00 <= o <= 0xD7AF:  # Hangul
            return True
    return False


def _tokenize_en_id(text: str) -> List[str]:
    return _EN_ID_WORD_RE.findall((text or "").strip())


def _is_word_token(tok: str) -> bool:
    return bool(re.match(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9']+$", tok))


def _is_punct(tok: str) -> bool:
    return len(tok) == 1 and not tok.isalnum() and not tok.isalpha() and tok.strip()


def _pick_best_from_lookup_str(s: str, *, tgt: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    parts = [p.strip() for p in _SPLIT_SEP_RE.split(s) if p.strip()]
    if not parts:
        return ""
    if tgt == "ja":
        for p in parts:
            if _has_cjk(p):
                return p[:40].rstrip() + ("…" if len(p) > 40 else "")
    best = parts[0]
    return best[:40].rstrip() + ("…" if len(best) > 40 else "")


def _assemble_ja(tokens: List[str]) -> str:
    # Glue Japanese tokens; keep spaces only for leftover roman tokens.
    out: List[str] = []
    for t in tokens:
        if not t:
            continue
        if _is_punct(t):
            out.append(t)
            continue
        if _has_cjk(t):
            out.append(t)
        else:
            if out and not out[-1].endswith(" "):
                out.append(" ")
            out.append(t)
            out.append(" ")
    s = "".join(out).strip()
    s = re.sub(r"\s{2,}", " ", s)
    # punctuation normalize
    s = s.replace(" .", "。").replace(".", "。")
    s = s.replace(" ,", "、").replace(",", "、")
    s = s.replace(" ?", "？").replace("?", "？")
    s = s.replace(" !", "！").replace("!", "！")
    s = s.replace(" 、", "、").replace(" 。", "。")
    return s


def _detect_src_sentence(text: str) -> str:
    low = (text or "").lower()
    if re.search(r"\b(saya|aku|dan|yang|tidak|nggak|gak|bukan)\b", low):
        return "id"
    return "en"


class _SentenceAssembler:
    """
    Small helper so we can unit-test / use without changing the lookup cache semantics.
    """
    def __init__(self, store: "LocalDictStore"):
        self.store = store

    def _lookup_token(self, tok: str, *, tgt: str) -> str:
        # Reuse existing cache pipeline (lookup_sync enforces is_short_input; tok is short)
        res = self.store.lookup_sync(tok, tgt)
        return _pick_best_from_lookup_str(res or "", tgt=tgt)

    def translate_id_to_ja(self, text: str) -> Tuple[str, List[Tuple[str, str]]]:
        toks = _tokenize_en_id(text)
        lowt = [t.lower() for t in toks]

        pairs: List[Tuple[str, str]] = []

        # Special-case: "Saya suka X dan Y" -> "私は XとY が好きです。"
        if len(lowt) >= 3 and lowt[0] in {"saya", "aku"} and "suka" in lowt:
            i_suka = lowt.index("suka")
            subject = "私"
            obj_tokens = toks[i_suka+1:]
            # Remove trailing punctuation from object parse
            obj_tokens_clean = []
            trailing_punct = ""
            for t in obj_tokens:
                if _is_punct(t):
                    trailing_punct = t
                    break
                obj_tokens_clean.append(t)

            obj_out: List[str] = []
            for t in obj_tokens_clean:
                lt = t.lower()
                if lt in _ID_TOKEN_MAP:
                    tr = _ID_TOKEN_MAP[lt]
                else:
                    tr = self._lookup_token(t, tgt="ja") or t
                pairs.append((t, tr))
                if tr:
                    obj_out.append(tr)

            obj_phrase = _assemble_ja(obj_out).replace("。","").replace("、","")
            # Ensure conjunctions look ok (we map dan->と)
            out = f"{subject}は{obj_phrase}が好きです。"
            return out, pairs

        # General fallback: token-wise map -> dict lookup -> assemble
        out_tokens: List[str] = []
        for t in toks:
            if _is_punct(t):
                out_tokens.append(t)
                continue
            lt = t.lower()
            if lt in _ID_TOKEN_MAP:
                tr = _ID_TOKEN_MAP[lt]
            else:
                tr = self._lookup_token(t, tgt="ja") or t
            pairs.append((t, tr))
            if tr:
                out_tokens.append(tr)

        out = _assemble_ja(out_tokens)
        return out, pairs

    def translate_en_to_ja(self, text: str) -> Tuple[str, List[Tuple[str, str]]]:
        toks = _tokenize_en_id(text)
        lowt = [t.lower() for t in toks]
        pairs: List[Tuple[str, str]] = []

        # Special-case: "I like X and Y" -> "私は XとY が好きです。"
        if lowt and lowt[0] in {"i", "we", "you"} and "like" in lowt:
            i_like = lowt.index("like")
            subject = _EN_TOKEN_MAP.get(lowt[0], "私")
            obj_tokens = toks[i_like+1:]
            obj_tokens_clean = []
            for t in obj_tokens:
                if _is_punct(t):
                    break
                obj_tokens_clean.append(t)

            obj_out: List[str] = []
            for t in obj_tokens_clean:
                lt = t.lower()
                if lt in _EN_TOKEN_MAP:
                    tr = _EN_TOKEN_MAP[lt]
                else:
                    tr = self._lookup_token(t, tgt="ja") or t
                pairs.append((t, tr))
                if tr:
                    obj_out.append(tr)

            obj_phrase = _assemble_ja(obj_out).replace("。","").replace("、","")
            out = f"{subject}は{obj_phrase}が好きです。"
            return out, pairs

        out_tokens: List[str] = []
        for t in toks:
            if _is_punct(t):
                out_tokens.append(t)
                continue
            lt = t.lower()
            if lt in _EN_TOKEN_MAP:
                tr = _EN_TOKEN_MAP[lt]
            else:
                tr = self._lookup_token(t, tgt="ja") or t
            pairs.append((t, tr))
            if tr:
                out_tokens.append(tr)

        out = _assemble_ja(out_tokens)
        return out, pairs