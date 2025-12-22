# -*- coding: utf-8 -*-
"""
c20_translate_commands_text.py

Text-only translate commands (guild-only):

- Handles lightweight message command:
    nixe translate ke <lang>
    <text...>

Supported target languages (strict):
- EN, JA/JP, KO/KR, ZH/CN, SU (Sunda), JV (Jawa), ID

Rules:
- Target=ID is skipped if the source is already Indonesian (anti-paraphrase).
- Does not touch image OCR / reverse image pipelines (those live in c20_translate_commands.py).
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Dict, List, Tuple

import discord
from discord.ext import commands

import os
import re
import httpx

# ----------------------------
# Self-contained text translate helpers
# (Do NOT import c20_translate_commands to keep split isolation)
# ----------------------------

def _env(k: str, default: str = "") -> str:
    try:
        v = os.getenv(k)
        return default if v is None else str(v)
    except Exception:
        return default

def _as_bool(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default

def _as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default

_LANG_MAP = {
    "en": ("EN", "English"),
    "english": ("EN", "English"),
    "id": ("ID", "Indonesian"),
    "indo": ("ID", "Indonesian"),
    "indonesia": ("ID", "Indonesian"),
    "indonesian": ("ID", "Indonesian"),
    "ja": ("JA", "Japanese"),
    "jp": ("JA", "Japanese"),
    "japan": ("JA", "Japanese"),
    "japanese": ("JA", "Japanese"),
    "ko": ("KO", "Korean"),
    "kr": ("KO", "Korean"),
    "korea": ("KO", "Korean"),
    "korean": ("KO", "Korean"),
    "zh": ("ZH", "Chinese (Simplified)"),
    "cn": ("ZH", "Chinese (Simplified)"),
    "chinese": ("ZH", "Chinese (Simplified)"),
    "mandarin": ("ZH", "Chinese (Simplified)"),
    "su": ("SU", "Sundanese"),
    "sunda": ("SU", "Sundanese"),
    "sundanese": ("SU", "Sundanese"),
    "jv": ("JV", "Javanese"),
    "jawa": ("JV", "Javanese"),
    "javanese": ("JV", "Javanese"),
}

def resolve_lang(raw: str) -> Tuple[str, str]:
    s = (raw or "").strip().lower()
    if not s:
        return ("ID", "Indonesian")
    if s in _LANG_MAP:
        return _LANG_MAP[s]
    # Accept formats like "ke EN", "to id", etc.
    s = re.sub(r"[^a-z]", "", s)
    if s in _LANG_MAP:
        return _LANG_MAP[s]
    return ("ID", "Indonesian")

_ID_HINT_WORDS = (
    "yang", "dan", "dengan", "untuk", "tidak", "bukan", "sudah", "belum", "bisa",
    "akan", "kamu", "saya", "kami", "mereka", "ini", "itu", "dari", "pada", "juga",
    "karena", "agar", "atau", "jadi", "sebagai", "lebih", "jadi", "mungkin",
)

def _looks_indonesian(text: str) -> bool:
    if not text:
        return False
    t = " " + re.sub(r"\s+", " ", text.strip().lower()) + " "
    hits = 0
    for w in _ID_HINT_WORDS:
        if f" {w} " in t:
            hits += 1
            if hits >= 2:
                return True
    # If it contains many common affixes, treat as Indonesian-ish
    aff = sum(1 for a in ("meng", "meny", "ber", "ter", "per", "ke", "se", "di", "pe") if a in t)
    return hits >= 1 and aff >= 2

def _should_skip_translation_for_id(src: str) -> bool:
    # Skip if it already appears to be Indonesian (anti-paraphrase)
    return _looks_indonesian(src)

def _pick_provider() -> str:
    return "gemini"

def _embed_add_long_field(embed: discord.Embed, title: str, value: str, *, max_inline_chars: int = 1024) -> Tuple[discord.Embed, List[discord.File]]:
    """Discord embed field values cap at 1024. If longer, attach as .txt and keep embed clean."""
    files: List[discord.File] = []
    if value is None:
        value = ""
    if len(value) <= max_inline_chars:
        embed.add_field(name=title, value=value or " ", inline=False)
        return embed, files

    # attach full text
    buf = io.BytesIO(value.encode("utf-8", errors="replace"))
    files.append(discord.File(buf, filename="translation_full.txt"))
    # show truncated preview
    preview = value[:max_inline_chars-20] + "\n…(lihat file translation_full.txt)"
    embed.add_field(name=title, value=preview, inline=False)
    return embed, files

async def _gemini_generate_text(*, api_key: str, model: str, prompt: str, timeout_sec: float) -> Tuple[bool, str]:
    if not api_key:
        return False, "Missing TRANSLATE_GEMINI_API_KEY"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.9,
            "maxOutputTokens": 2048,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            r = await client.post(url, json=payload)
        if r.status_code >= 400:
            # Keep a short diagnostic
            try:
                j = r.json()
                msg = j.get("error", {}).get("message") or r.text
            except Exception:
                msg = r.text
            msg = (msg or "").strip()
            return False, f"Gemini API error {r.status_code}: {msg[:200]}"
        j = r.json()
        # standard response: candidates[0].content.parts[0].text
        cand = (j.get("candidates") or [{}])[0]
        content = (cand.get("content") or {})
        parts = content.get("parts") or []
        out = ""
        for p in parts:
            if isinstance(p, dict) and "text" in p:
                out += str(p.get("text") or "")
        out = (out or "").strip()
        if not out:
            return False, "Empty response from Gemini"
        return True, out
    except Exception as e:
        return False, f"Gemini request failed: {type(e).__name__}: {e}"

async def _gemini_translate_text(text: str, target_lang: str) -> Tuple[bool, str]:
    # Keep model selection strict and modern; allow override.
    model = (_env("TRANSLATE_GEMINI_MODEL", "gemini-2.5-flash-lite").strip() or "gemini-2.5-flash-lite")
    api_key = _env("TRANSLATE_GEMINI_API_KEY", "").strip()
    timeout = _as_float(_env("TRANSLATE_TEXT_TIMEOUT_SEC", "25"), 25.0)
    timeout = max(5.0, min(60.0, timeout))

    # Translate-only prompt; no explanations.
    prompt = (
        f"You are a translation engine. Translate the text to {target_lang}. "
        "Return ONLY the translated text. Preserve line breaks. Do not add commentary.\n\n"
        f"TEXT:\n{text}"
    )
    ok, out = await _gemini_generate_text(api_key=api_key, model=model, prompt=prompt, timeout_sec=timeout)
    return ok, out

async def _gemini_translate_text_ja_multi(text: str) -> Tuple[bool, str]:
    return await _gemini_translate_text(text, "Japanese")

async def _gemini_translate_text_ko_multi(text: str) -> Tuple[bool, str]:
    return await _gemini_translate_text(text, "Korean")

async def _gemini_translate_text_zh_multi(text: str) -> Tuple[bool, str]:
    return await _gemini_translate_text(text, "Chinese (Simplified)")

log = logging.getLogger(__name__)


class TranslateCommandsText(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_call: Dict[int, float] = {}

    def _cooldown_ok(self, user_id: int) -> Tuple[bool, float]:
        cd = _as_float("TRANSLATE_COOLDOWN_SEC", 5.0)
        now = asyncio.get_event_loop().time()
        last = self._last_call.get(user_id, 0.0)
        if now - last < cd:
            return False, cd - (now - last)
        self._last_call[user_id] = now
        return True, 0.0

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        Lightweight text command handler: "nixe translate ke <lang> ..." (no slash).

        Examples:
          nixe translate ke jp
          please, marry me

          nixe translate ke id please translate this

        Behaviour:
        - Only works in guild text channels.
        - Respects TRANSLATE_TEXT_ENABLE and cooldown (TRANSLATE_COOLDOWN_SEC).
        - Uses the same Gemini pipelines as context-menu translate, but only for plain text
          (no embed/image handling here).
        """
        try:
            if message.guild is None:
                return
            if message.author.bot:
                return
            if not _as_bool("TRANSLATE_ENABLE", True):
                return
            if not _as_bool("TRANSLATE_TEXT_ENABLE", True):
                return

            content = (message.content or "").strip()
            if not content:
                return

            low = content.lower()
            prefix = "nixe translate"
            if not low.startswith(prefix):
                return

            # Cooldown per user
            ok_cd, wait_s = self._cooldown_ok(message.author.id)
            if not ok_cd:
                # Ringan saja, jangan spam error.
                return

            # Split baris; baris pertama command, sisanya teks.
            lines = content.splitlines()
            first = lines[0]
            rest_lines = lines[1:]

            tokens = first.split()
            # tokens minimal: ["nixe", "translate"]
            target_token = None
            # cari pola "ke <lang>"
            for i, tok in enumerate(tokens):
                if tok.lower() == "ke" and i + 1 < len(tokens):
                    target_token = tokens[i + 1]
                    break
            if target_token is None and len(tokens) >= 3:
                # fallback: anggap token ke-3 adalah kode bahasa
                target_token = tokens[2]

            # Mapping token ke nama bahasa yang dipakai oleh model
            def _map_lang_token(tok: str) -> str:
                t = (tok or "").strip().lower()
                if not t:
                    return ""
                if t in ("jp", "ja", "jpn", "japanese", "nihongo", "日本語"):
                    return "Japanese"
                if t in ("kr", "ko", "kor", "korean", "hangul", "한국어"):
                    return "Korean"
                if t in ("cn", "zh", "zho", "chi", "chinese", "mandarin", "中文", "汉语", "漢語"):
                    return "Chinese"
                if t in ("id", "indo", "ind", "indonesian", "bahasa indonesia", "bahasa"):
                    return "Indonesian"
                if t in ("en", "eng", "english"):
                    return "English"
                if t in ("su", "sun", "sunda", "sundanese", "bahasa sunda"):
                    return "Sundanese"
                if t in ("jv", "jav", "jawa", "javanese", "bahasa jawa"):
                    return "Javanese"
                if t in ("ar", "arab", "arabic", "العربية"):
                    return "Arabic"
                # fallback: pakai apa adanya
                return tok

            default_target = _env("TRANSLATE_TARGET_LANG", "id")
            target_lang = _map_lang_token(target_token or default_target)

            # Tentukan teks yang ingin diterjemahkan.
            if rest_lines:
                text_to_translate = "\n".join(rest_lines).strip()
            else:
                # Ambil token setelah bahasa sebagai teks inline.
                start_idx = None
                if target_token is not None:
                    # cari posisi pertama target_token di tokens
                    for i, tok in enumerate(tokens):
                        if tok == target_token:
                            start_idx = i + 1
                            break
                else:
                    # setelah "nixe translate"
                    start_idx = 2
                if start_idx is not None and start_idx < len(tokens):
                    text_to_translate = " ".join(tokens[start_idx:]).strip()
                else:
                    text_to_translate = ""

            # Kalau teks kosong, coba ambil dari message yang di-reply.
            if not text_to_translate:
                ref = message.reference
                if ref and isinstance(ref.resolved, discord.Message):
                    ref_msg = ref.resolved
                    text_to_translate = (ref_msg.content or "").strip()

            if not text_to_translate:
                await message.channel.send(
                    "Tidak ada teks yang bisa diterjemahkan dari perintah ini.",
                    reference=message,
                )
                return

            # Resolve profil bahasa untuk label dan multi-style.
            prof = resolve_lang(target_lang)
            if prof is not None:
                target_display = prof.display
                target_code = prof.code.lower()
            else:
                target_display = str(target_lang)
                target_code = str(target_lang or "").strip().lower()

            # Restrict supported targets to avoid unexpected outputs / config drift.
            # Allowed: EN, JA/JP, KO/KR, ZH/CN, SU (Sunda), JV (Jawa), and ID.
            _allowed_prefix = ("en", "ja", "ko", "zh", "su", "jv", "id")
            if not any(target_code == p or target_code.startswith(p + "-") for p in _allowed_prefix):
                await message.channel.send(
                    f"Bahasa target tidak didukung: `{target_display}`. "
                    f"Yang didukung: EN/JP/KR/CN/SUNDA/JAWA/ID.",
                    reference=message,
                )
                return
            provider = _pick_provider()  # saat ini selalu "gemini"

            # Jalur multi-style untuk JA/KO/ZH jika diaktifkan.
            try:
                if target_code.startswith("ja") and _as_bool("TRANSLATE_JA_DUAL_ENABLE", True):
                    ok_multi, data = await _gemini_translate_text_ja_multi(text_to_translate)
                    if not ok_multi:
                        await message.channel.send(data.get("reason", "Gagal translate ke Jepang."), reference=message)
                        return
                    formal = (data.get("formal") or "").strip()
                    casual = (data.get("casual") or "").strip()
                    romaji = (data.get("romaji") or "").strip() if _as_bool("TRANSLATE_JA_ROMAJI_ENABLE", True) else ""
                    wuwa = (data.get("wuwa") or "").strip()
                    wuwa_romaji = (data.get("wuwa_romaji") or "").strip() if _as_bool("TRANSLATE_JA_ROMAJI_ENABLE", True) else ""
                    embed = discord.Embed(title=f"Translation → {target_display}")
                    leftover = _embed_add_long_field(embed, "Formal (Global)", (formal or "(empty)"), inline=False)
                    if leftover:
                        attachments_text.append("\n\n[Formal (Global)]\n" + leftover)
                    leftover = _embed_add_long_field(embed, "Casual (Global)", (casual or "(empty)"), inline=False)
                    if leftover:
                        attachments_text.append("\n\n[Casual (Global)]\n" + leftover)
                    if romaji:
                        leftover = _embed_add_long_field(embed, "Romaji (Global)", romaji, inline=False)
                        if leftover:
                            attachments_text.append("\n\n[Romaji (Global)]\n" + leftover)
                    if wuwa:
                        leftover = _embed_add_long_field(embed, "WuWa Gamer", wuwa, inline=False)
                        if leftover:
                            attachments_text.append("\n\n[WuWa Gamer]\n" + leftover)
                    if wuwa_romaji:
                        embed.add_field(name="WuWa Romaji", value=wuwa_romaji[:1024], inline=False)
                elif target_code.startswith("ko") and _as_bool("TRANSLATE_KO_DUAL_ENABLE", True):
                    ok_multi, data = await _gemini_translate_text_ko_multi(text_to_translate)
                    if not ok_multi:
                        await message.channel.send(data.get("reason", "Gagal translate ke Korea."), reference=message)
                        return
                    formal = (data.get("formal") or "").strip()
                    casual = (data.get("casual") or "").strip()
                    embed = discord.Embed(title=f"Translation → {target_display}")
                    embed.add_field(name="Formal", value=(formal or "(empty)")[:1024], inline=False)
                    embed.add_field(name="Casual", value=(casual or "(empty)")[:1024], inline=False)
                elif target_code.startswith("zh") and _as_bool("TRANSLATE_ZH_DUAL_ENABLE", True):
                    ok_multi, data = await _gemini_translate_text_zh_multi(text_to_translate)
                    if not ok_multi:
                        await message.channel.send(data.get("reason", "Gagal translate ke Chinese."), reference=message)
                        return
                    formal = (data.get("formal") or "").strip()
                    casual = (data.get("casual") or "").strip()
                    embed = discord.Embed(title=f"Translation → {target_display}")
                    embed.add_field(name="Formal", value=(formal or "(empty)")[:1024], inline=False)
                    embed.add_field(name="Casual", value=(casual or "(empty)")[:1024], inline=False)
                else:
                    if _should_skip_translation_for_id(text_to_translate, target_lang):
                        translated = (text_to_translate or "").strip() or "(empty)"
                    else:
                        ok_single, out_single = await _gemini_translate_text(text_to_translate, target_lang)
                        if not ok_single:
                            await message.channel.send(out_single, reference=message)
                            return
                        translated = (out_single or "").strip()
                    embed = discord.Embed(title="Translation")
                    attachments_text: List[str] = []
                    leftover = _embed_add_long_field(embed, "Source", (text_to_translate or "(empty)"), inline=False)
                    if leftover:
                        attachments_text.append("\n\n[Source]\n" + leftover)
                    leftover = _embed_add_long_field(embed, f"Translated → {target_display}", (translated or "(empty)"), inline=False)
                    if leftover:
                        attachments_text.append(f"\n\n[Translated → {target_display}]\n" + leftover)

                footer_bits = [f"text={provider}", "image=gemini", f"target={target_lang}"]
                embed.set_footer(text=" • ".join(footer_bits))

                files = None
                if attachments_text:
                    blob = "\n\n".join([t for t in attachments_text if t and t.strip()]).strip()
                    if blob:
                        files = [discord.File(fp=io.BytesIO(blob.encode("utf-8", errors="ignore")), filename="translation_full.txt")]
                if files:
                    await message.channel.send(embed=embed, files=files, reference=message)
                else:
                    await message.channel.send(embed=embed, reference=message)
            except Exception as e:
                log.exception("[translate] text-command failed: %s", e)
                await message.channel.send("Terjadi error saat translate teks ini.", reference=message)
        except Exception:
            # Jangan pernah biarkan error di on_message bocor keluar.
            log.exception("[translate] on_message handler crashed")

async def setup(bot: commands.Bot):
    await bot.add_cog(TranslateCommandsText(bot))
