
from __future__ import annotations
import os
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

log = logging.getLogger("nixe.config.env")

def load_dotenv_verbose() -> None:
    try:
        from dotenv import load_dotenv, find_dotenv
        path = find_dotenv(usecwd=True)
        if path:
            load_dotenv(path); print(f"✅ Loaded env file: {path}")
        else:
            env_path = os.path.join(os.getcwd(), ".env")
            if os.path.exists(env_path):
                load_dotenv(env_path); print(f"✅ Loaded env file: {env_path}")
    except Exception:
        return

@dataclass(frozen=True)
class Settings:
    MODE: str = os.getenv("NIXE_MODE", os.getenv("MODE", "production"))
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "10000"))
    ACCESS_LOG: bool = os.getenv("ACCESS_LOG", "1") not in {"0","false","False"}

    DISCORD_TOKEN: Optional[str] = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")

    # Baked-in IDs from user
    PHASH_DB_THREAD_ID: int = int(os.getenv("PHASH_DB_THREAD_ID", "1431192568221270108"))
    PHASH_SOURCE_THREAD_ID: int = int(os.getenv("PHASH_SOURCE_THREAD_ID", "1409949797313679492"))
    PHASH_IMPORT_SOURCE_THREAD_ID: int = int(os.getenv("PHASH_IMPORT_SOURCE_THREAD_ID", "1409949797313679492"))
    PHASH_IMAGEPHISH_THREAD_ID: int = int(os.getenv("PHASH_IMAGEPHISH_THREAD_ID", "1409949797313679492"))

    # Still required from you:
    PHASH_INBOX_CHANNEL_ID: int = int(os.getenv("PHASH_INBOX_CHANNEL_ID", "0"))

    PHASH_DB_MARKER: str = os.getenv("PHASH_DB_MARKER", "[phash-blacklist-db]")
    PHASH_DB_BOARD_MARKER: str = os.getenv("PHASH_DB_BOARD_MARKER", "[phash-db-board]")
    PHASH_DB_BOARD_EVERY_SEC: int = int(os.getenv("PHASH_DB_BOARD_EVERY_SEC", "300"))
    PHASH_DB_SCAN_LIMIT: int = int(os.getenv("PHASH_DB_SCAN_LIMIT", "12000"))
    PHASH_MATCH_THRESHOLD: float = float(os.getenv("PHASH_MATCH_THRESHOLD", "0.92"))
    PHASH_BAN_ON_MATCH: bool = os.getenv("PHASH_BAN_ON_MATCH", "1") not in {"0","false","False"}

    def token(self) -> Optional[str]:
        return self.DISCORD_TOKEN

@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()