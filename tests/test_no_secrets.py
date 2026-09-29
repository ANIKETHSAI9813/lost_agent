"""SPEC 11: "No secrets in the repo (grep test for key patterns)".

Two assertions:
  1. Credential files (`env`, `.env`, `*.env`) are git-ignored.
  2. Every other file is free of real key material.

Failure messages report the FILE and the PATTERN NAME only. A secret-finding
test that echoes the match would copy the key into CI logs, which is the exact
failure this test exists to prevent.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    "dist",
    "build",
    "htmlcov",
}

# name -> regex. Patterns require a realistic length so prose and examples
# ("gsk_<paste>", "<paste>") do not trip the test.
KEY_PATTERNS: dict[str, re.Pattern[str]] = {
    "groq_key": re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    "hindsight_key": re.compile(r"hsk_[A-Za-z0-9_\-]{20,}"),
    "openai_key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
}

# Files that are allowed to hold credentials, because they are the user's local
# secret store and must never be committed.
CREDENTIAL_FILENAMES = {"env", ".env"}

IGNORED_FILENAME_SUFFIXES = (".env",)


def _is_credential_file(path: Path) -> bool:
    return path.name in CREDENTIAL_FILENAMES or path.name.endswith(
        IGNORED_FILENAME_SUFFIXES
    )


def _scannable_files() -> list[Path]:
    out: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(PROJECT_ROOT).parts):
            continue
        if _is_credential_file(path):
            continue
        out.append(path)
    return out


def _gitignore_text() -> str:
    gitignore = PROJECT_ROOT / ".gitignore"
    if not gitignore.is_file():
        return ""
    return gitignore.read_text(encoding="utf-8", errors="replace")


def test_gitignore_covers_credential_files() -> None:
    """SPEC 10.1 requires .gitignore to include `.env`."""
    text = _gitignore_text()
    assert text, ".gitignore is missing or empty"
    lines = {line.strip() for line in text.splitlines()}
    # `env` matters as much as `.env` here: this project ships its template as
    # `env` with no dot, so a `.env`-only pattern would not protect it.
    assert ".env" in lines, ".gitignore must list .env"
    assert "env" in lines, ".gitignore must list env (this project uses that name)"


def test_no_key_material_outside_credential_files() -> None:
    """No committed-eligible file may contain a real-looking key."""
    offenders: list[str] = []
    for path in _scannable_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: not a source file
        for name, pattern in KEY_PATTERNS.items():
            if pattern.search(text):
                rel = path.relative_to(PROJECT_ROOT)
                # Never include the matched text itself.
                offenders.append(f"{rel} (pattern: {name})")
    assert not offenders, "possible secrets found (values withheld): " + ", ".join(
        offenders
    )


def test_example_env_has_no_values() -> None:
    """`.env.example` is committed, so it must carry empty values only."""
    example = PROJECT_ROOT / ".env.example"
    assert example.is_file(), ".env.example is missing"
    for line in example.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        value = value.strip().strip('"').strip("'")
        if any(token in name for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            assert value == "", f".env.example must leave {name.strip()} empty"


def _load_make_release():
    """Import scripts/make_release.py without requiring `scripts` on sys.path."""
    spec = importlib.util.spec_from_file_location(
        "make_release", PROJECT_ROOT / "scripts" / "make_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_release_zip_is_secret_free_and_excludes_secrets_and_legacy() -> None:
    """The hand-off archive must never carry a live key or the dead page.

    Builds the same artifact `scripts/make_release.py` produces and scans it
    with the byte-level key patterns. Assertions report the FILE and PATTERN
    NAME only, never the matched text.
    """
    module = _load_make_release()
    files = module.iter_project_files()
    names = {path.relative_to(PROJECT_ROOT).as_posix() for path in files}

    assert "env" not in names
    assert ".env" not in names
    assert "static/index_legacy.html" not in names
    assert ".env.example" in names, "release must ship the template"

    module.build_archive(files, module.RELEASE_ZIP)
    offenders = module.scan_archive_bytes(module.RELEASE_ZIP)
    assert not offenders, (
        "possible secrets in release archive (values withheld): "
        + ", ".join(offenders)
    )
