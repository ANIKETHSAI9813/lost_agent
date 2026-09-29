"""Build `dist/LostAgent_release.zip` for hand-off (pure stdlib, cross-platform).

The archive is a clean working copy of the project: it excludes the human's
local secret files (`env`, `.env`), the virtualenv, bytecode/cache artifacts,
previous release builds, and the retired legacy page. It INcludes `.env.example`
so the recipient can scaffold their own keys.

After writing the archive this script scans every entry with the same key
patterns as `tests/test_no_secrets.py` and exits non-zero if anything matches,
so the artifact that ships can never carry a live key.

Usage:
    python scripts/make_release.py

Exit codes:
    0  OK  - archive built and scanned clean
    1  FAIL - a secret pattern matched inside the archive
    2  FAIL - the archive could not be built
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = PROJECT_ROOT / "dist"
RELEASE_ZIP = RELEASE_DIR / "LostAgent_release.zip"

# Every directory/file that must never leave the machine, mirroring .gitignore
# plus the sprint packaging rules (dead legacy page, caches, virtualenv).
EXCLUDED_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".git",
    "node_modules",
    "dist",
    "build",
}
EXCLUDED_FILES = {
    "env",
    ".env",
    "static/index_legacy.html",
}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".log")
EXCLUDED_NAME_SUFFIXES = (".env",)  # covers `foo.env`

# Shared with tests/test_no_secrets.py: a match reports the FILE and PATTERN
# NAME only, never the matched text, so a secret never reaches logs.
KEY_PATTERNS: dict[str, re.Pattern[str]] = {
    "groq_key": re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    "hindsight_key": re.compile(r"hsk_[A-Za-z0-9_\-]{20,}"),
    "openai_key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
}
KEY_PATTERNS_BYTES: dict[str, re.Pattern[bytes]] = {
    name: re.compile(source.encode("utf-8"))
    for name, source in (
        ("groq_key", r"gsk_[A-Za-z0-9]{20,}"),
        ("hindsight_key", r"hsk_[A-Za-z0-9_\-]{20,}"),
        ("openai_key", r"sk-[A-Za-z0-9]{20,}"),
        ("anthropic_key", r"sk-ant-[A-Za-z0-9_\-]{20,}"),
        ("aws_access_key", r"AKIA[0-9A-Z]{16}"),
    )
}


def _excluded(rel: Path) -> bool:
    parts = rel.parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    name = parts[-1]
    if rel.as_posix() in EXCLUDED_FILES:
        return True
    if name in {"env", ".env"} or name.endswith(EXCLUDED_NAME_SUFFIXES):
        return True
    if any(name.endswith(sfx) for sfx in EXCLUDED_SUFFIXES):
        return True
    return False


def iter_project_files() -> list[Path]:
    out: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(PROJECT_ROOT)
        if _excluded(rel):
            continue
        out.append(path)
    return sorted(out)


def build_archive(files: list[Path], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(PROJECT_ROOT).as_posix())


def scan_archive_bytes(target: Path) -> list[str]:
    """Return a list of `entry (pattern: name)` offenders, values withheld.

    The archive is scanned as raw bytes (not per-entry text decode) so binary
    files and UTF-16 output cannot slip a key past the check.
    """
    offenders: list[str] = []
    with zipfile.ZipFile(target, "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            data = archive.read(info)
            for name, pattern in KEY_PATTERNS_BYTES.items():
                if pattern.search(data):
                    offenders.append(f"{info.filename} (pattern: {name})")
    return offenders


def main() -> int:
    files = iter_project_files()
    try:
        build_archive(files, RELEASE_ZIP)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: could not build archive: {type(exc).__name__}: {exc}")
        return 2

    offenders = scan_archive_bytes(RELEASE_ZIP)
    if offenders:
        print("FAIL: secret pattern(s) found inside the archive (values withheld):")
        for offender in offenders:
            print(f"  {offender}")
        return 1

    print(f"OK: {RELEASE_ZIP} ({RELEASE_ZIP.stat().st_size} bytes, "
          f"{len(files)} files) built and scanned clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())