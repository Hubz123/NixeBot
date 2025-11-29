from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class LangProfile:
    code: str
    display: str
    aliases: List[str]


LANG_PROFILES: Dict[str, LangProfile] = {
    "ja": LangProfile("ja", "JA", ["ja", "ja-jp", "japanese", "日本語"]),
    "ko": LangProfile("ko", "KO", ["ko", "ko-kr", "korean", "hangul", "한국어"]),
    "zh": LangProfile("zh", "ZH", ["zh", "zh-cn", "zh-hans", "zh-hant", "chinese", "中文", "mandarin"]),
    "ar": LangProfile("ar", "AR", ["ar", "ar-sa", "arabic", "العربية"]),
}


def resolve_lang(target: str) -> Optional[LangProfile]:
    t = (target or "").strip().lower()
    if not t:
        return None
    for prof in LANG_PROFILES.values():
        for alias in prof.aliases:
            if t == alias.lower():
                return prof
    prof = LANG_PROFILES.get(t)
    if prof is not None:
        return prof
    return None
