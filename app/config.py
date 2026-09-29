"""Runtime configuration.

Env files are read at runtime only and their values are never logged or echoed
(SPEC 0 / SPEC 11 "no secrets in the repo"). Only `describe()` is exposed, and
it reports booleans and lengths, never contents.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
SEED_DIR = BASE_DIR / "seed"
STATIC_DIR = BASE_DIR / "static"

# SPEC 5.2 fixes the known competitor names: Acme, Vantage. There is no
# equivalent field in scoring_config.json, so it lives here as a named
# constant (DECISIONS.md D6: the name mention is itself the extraction cue).
KNOWN_COMPETITORS: tuple[str, ...] = ("Acme", "Vantage")

# SPEC 4: `clip(x) = min(max(x, 0.05), 0.95)`. NOT in scoring_config.json --
# added deliberately so a 5-lost/0-won ratio cannot produce ln(inf).
# Documented in the README as a documented addition.
CLIP_MIN = 0.05
CLIP_MAX = 0.95


def _load_env_files() -> list[str]:
    """Load `.env` and `env` if present. Returns the filenames that existed.

    This project ships its template as `env` (no dot), while dotenv convention
    is `.env`, so both are supported. Neither file is modified or printed.
    """
    loaded: list[str] = []
    for name in (".env", "env"):
        path = BASE_DIR / name
        if path.is_file():
            load_dotenv(path, override=False)
            loaded.append(name)
    return loaded


ENV_FILES_LOADED = _load_env_files()


def _get(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _get_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class Settings:
    memory_backend: str = "hindsight"
    hindsight_api_key: str = ""
    hindsight_base_url: str = ""
    hindsight_bank_id: str = "sales-team-shared"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com"
    env_files_loaded: tuple[str, ...] = field(default_factory=tuple)
    # D35: a fresh install starts each demo from exactly the seed by retiring
    # any `live_added` deals left in shared memory at startup.
    demo_clean_start: bool = True

    @property
    def hindsight_configured(self) -> bool:
        """True only when both credential and endpoint are present.

        SPEC 6: never show a Hindsight badge unless calls actually succeeded,
        so configuration alone is never enough to claim Hindsight is active.
        """
        return bool(self.hindsight_api_key and self.hindsight_base_url)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.groq_api_key)

    def describe(self) -> dict:
        """Non-secret status only. Never returns a key, prefix or length."""
        return {
            "memory_backend_requested": self.memory_backend,
            "hindsight_configured": self.hindsight_configured,
            "hindsight_bank_id": self.hindsight_bank_id,
            "groq_configured": self.llm_enabled,
            "groq_model": self.groq_model if self.llm_enabled else None,
            "demo_clean_start": self.demo_clean_start,
            "env_files_loaded": list(self.env_files_loaded),
        }


settings = Settings(
    memory_backend=_get("MEMORY_BACKEND", "hindsight") or "hindsight",
    hindsight_api_key=_get("HINDSIGHT_API_KEY"),
    hindsight_base_url=_get("HINDSIGHT_BASE_URL"),
    hindsight_bank_id=_get("HINDSIGHT_BANK_ID", "sales-team-shared")
    or "sales-team-shared",
    groq_api_key=_get("GROQ_API_KEY"),
    groq_model=_get("GROQ_MODEL", "openai/gpt-oss-120b") or "openai/gpt-oss-120b",
    groq_base_url=_get("GROQ_BASE_URL", "https://api.groq.com")
    or "https://api.groq.com",
    env_files_loaded=tuple(ENV_FILES_LOADED),
    demo_clean_start=_get_bool("DEMO_CLEAN_START", True),
)
