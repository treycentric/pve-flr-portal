"""Single source of truth for the app's displayed version + repo link.

The VERSION file at the repo root is the one place the version number
lives - scripts/release.py updates it, this module just reads it. Kept
as a plain text file (not embedded in code) so shell/CI tooling can read
it without importing Python (see docs/dev/versioning.md).
"""
from pathlib import Path

REPO_URL = "https://github.com/treycentric/pve-flr-portal"

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def _read_version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0-unknown"


__version__ = _read_version()
