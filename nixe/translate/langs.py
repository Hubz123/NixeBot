from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class LangProfile:
    code: str
    display: str
    aliases: List[str]


LANG_PROFILES: Dict[str, LangProfile] = {
    # Japanese
    "ja": LangProfile("ja", "JA", ["ja", "ja-jp", "jp", "japanese", "日本語"]),
    # Korean
    "ko": LangProfile("ko", "KO", ["ko", "ko-kr", "kr", "korean", "hangul", "한국어"]),
    # Chinese (generic, covers simplified/traditional/mandarin)
    "zh": LangProfile("zh", "ZH", ["zh", "zh-cn", "zh-hans", "zh-hant", "cn", "chinese", "mandarin", "中文", "汉语", "漢語"]),
    # Arabic
    "ar": LangProfile("ar", "AR", ["ar", "ar-sa", "arabic", "العربية"]),
    # Indonesian
    "id": LangProfile("id", "ID", ["id", "id-id", "indonesian", "bahasa indonesia", "indo"]),
    # English
    "en": LangProfile("en", "EN", ["en", "en-us", "en-gb", "english"]),
    # Sundanese
    "su": LangProfile("su", "SUN", ["su", "sun", "sunda", "sundanese", "bahasa sunda"]),
    # Javanese
    "jv": LangProfile("jv", "JAWA", ["jv", "jav", "jawa", "javanese", "bahasa jawa"]),
}


def resolve_lang(target: str) -> Optional[LangProfile]:
    t = (target or "").strip().lower()
    if not t:
        return None
    # match against aliases first
    for prof in LANG_PROFILES.values():
        for alias in prof.aliases:
            if t == alias.lower():
                return prof
    # then fall back to direct key
    prof = LANG_PROFILES.get(t)
    if prof is not None:
        return prof
    return None
