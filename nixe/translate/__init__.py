"""
Lightweight language metadata package for Nixe translate.
"""
from .langs import LangProfile, LANG_PROFILES, resolve_lang

__all__ = ["LangProfile", "LANG_PROFILES", "resolve_lang"]
