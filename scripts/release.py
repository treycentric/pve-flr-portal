#!/usr/bin/env python3
"""Version/changelog/release automation. See docs/dev/versioning.md for
the full convention this implements (Conventional Commits -> SemVer ->
Keep a Changelog).

Subcommands:
  suggest   Preview the recommended bump + changelog draft. Read-only.
  bump      Update VERSION + CHANGELOG.md, commit, and tag.
  release   Create a GitHub release from a CHANGELOG.md section via `gh`.

Pure logic (commit parsing, bump recommendation, changelog formatting) is
kept separate from git/filesystem I/O so it can be unit tested without a
real repo - see tests/test_release.py.
"""
from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"

CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s*(?P<description>.+)$"
)

# Conventional Commit type -> Keep a Changelog section. Anything not
# listed here and not in IGNORED_TYPES falls back to "Changed".
TYPE_TO_SECTION = {
    "feat": "Added",
    "fix": "Fixed",
    "perf": "Changed",
    "refactor": "Changed",
    "revert": "Changed",
    "security": "Security",
}
# Not user-facing - excluded from the changelog entirely.
IGNORED_TYPES = {"chore", "docs", "style", "test", "ci", "build"}

SECTION_ORDER = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]

_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"


# --------------------------------------------------------------------------
# Pure logic - no subprocess, no filesystem. Unit tested directly.
# --------------------------------------------------------------------------


@dataclass
class Commit:
    hash: str
    subject: str
    body: str = ""

    @property
    def _match(self):
        return CONVENTIONAL_COMMIT_RE.match(self.subject.strip())

    @property
    def type(self) -> str | None:
        m = self._match
        return m.group("type").lower() if m else None

    @property
    def scope(self) -> str | None:
        m = self._match
        return m.group("scope") if m else None

    @property
    def description(self) -> str:
        m = self._match
        return m.group("description") if m else self.subject.strip()

    @property
    def is_breaking(self) -> bool:
        m = self._match
        if m and m.group("breaking"):
            return True
        return "BREAKING CHANGE:" in self.body or "BREAKING-CHANGE:" in self.body

    @property
    def breaking_description(self) -> str | None:
        for marker in ("BREAKING CHANGE:", "BREAKING-CHANGE:"):
            if marker in self.body:
                rest = self.body.split(marker, 1)[1].strip()
                return rest.splitlines()[0].strip() if rest else self.description
        return self.description if self.is_breaking else None


def recommend_bump(commits: list[Commit]) -> str | None:
    """SemVer bump implied by a set of commits, per the Conventional
    Commits spec: any breaking change -> major; else any feat -> minor;
    else any fix/perf/revert -> patch; else None (nothing user-facing)."""
    if any(c.is_breaking for c in commits):
        return "major"
    if any(c.type == "feat" for c in commits):
        return "minor"
    if any(c.type in ("fix", "perf", "revert") for c in commits):
        return "patch"
    return None


def next_version(current: str, bump: str) -> str:
    try:
        major, minor, patch = (int(p) for p in current.split("."))
    except ValueError as e:
        raise ValueError(f"VERSION file does not contain a MAJOR.MINOR.PATCH value: {current!r}") from e
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump kind: {bump!r} (expected major/minor/patch)")


def build_changelog_sections(commits: list[Commit]) -> dict[str, list[str]]:
    """Groups commit descriptions into Keep a Changelog sections. Order of
    entries within a section follows commit order (oldest first, matching
    how commits_since() returns them)."""
    sections: dict[str, list[str]] = {}
    breaking_notes: list[str] = []
    for c in commits:
        if c.is_breaking and c.breaking_description:
            breaking_notes.append(c.breaking_description)
        if c.type in IGNORED_TYPES:
            continue
        section = TYPE_TO_SECTION.get(c.type, "Changed" if c.type else None)
        if section is None:
            continue
        scope_prefix = f"**{c.scope}:** " if c.scope else ""
        suffix = f" ({c.hash[:7]})" if c.hash else ""
        sections.setdefault(section, []).append(f"{scope_prefix}{c.description}{suffix}")
    if breaking_notes:
        sections["Breaking Changes"] = breaking_notes
    return sections


def format_changelog_entry(version: str, sections: dict[str, list[str]], when: date) -> str:
    lines = [f"## [{version}] - {when.isoformat()}", ""]
    ordered_keys = (["Breaking Changes"] if "Breaking Changes" in sections else []) + [
        s for s in SECTION_ORDER if s in sections
    ]
    if not ordered_keys:
        lines.append("_No user-facing changes._")
        lines.append("")
    for key in ordered_keys:
        lines.append(f"### {key}")
        for item in sections[key]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def extract_changelog_section(text: str, version: str) -> str | None:
    """Returns the body text under `## [version] - ...` up to (not
    including) the next `## [` heading, or None if that version has no
    section."""
    pattern = re.compile(rf"^## \[{re.escape(version)}\].*$", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return None
    start = m.end()
    next_m = re.search(r"^## \[", text[start:], re.MULTILINE)
    end = start + next_m.start() if next_m else len(text)
    return text[start:end].strip()


# --------------------------------------------------------------------------
# Filesystem I/O - paths are parameters (defaulting to the real repo files)
# so tests can point these at a tmp_path instead.
# --------------------------------------------------------------------------


def read_version(path: Path = VERSION_FILE) -> str:
    return path.read_text(encoding="utf-8").strip()


def write_version(version: str, path: Path = VERSION_FILE) -> None:
    path.write_text(version + "\n", encoding="utf-8")


def prepend_changelog(entry: str, path: Path = CHANGELOG_FILE) -> None:
    """Inserts `entry` as the newest version section, directly after the
    Keep a Changelog preamble and before the first existing `## [` entry
    (or at the end of the file if this is the very first entry)."""
    text = path.read_text(encoding="utf-8") if path.exists() else "# Changelog\n\n"
    marker = "\n## ["
    idx = text.find(marker)
    if idx == -1:
        new_text = text.rstrip("\n") + "\n\n" + entry
    else:
        new_text = text[: idx + 1] + entry + "\n" + text[idx + 1 :]
    path.write_text(new_text, encoding="utf-8")


# --------------------------------------------------------------------------
# Git plumbing.
# --------------------------------------------------------------------------


def _run(*args: str) -> str:
    result = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return result.stdout


def latest_tag() -> str | None:
    try:
        return _run("git", "describe", "--tags", "--abbrev=0").strip()
    except subprocess.CalledProcessError:
        return None


def commits_since(ref: str | None) -> list[Commit]:
    range_arg = f"{ref}..HEAD" if ref else "HEAD"
    fmt = f"%H{_FIELD_SEP}%s{_FIELD_SEP}%b{_RECORD_SEP}"
    out = _run("git", "log", range_arg, f"--pretty=format:{fmt}")
    commits = []
    for record in out.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(_FIELD_SEP)
        if len(parts) < 2:
            continue
        commit_hash, subject = parts[0], parts[1]
        body = parts[2] if len(parts) > 2 else ""
        # git log newest-first; changelog reads better oldest-first.
        commits.insert(0, Commit(hash=commit_hash, subject=subject, body=body))
    return commits


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cmd_suggest(args: argparse.Namespace) -> None:
    ref = latest_tag()
    commits = commits_since(ref)
    bump = recommend_bump(commits)
    current = read_version()
    print(f"Commits since {ref or 'repo start'}: {len(commits)}")
    print(f"Recommended bump: {bump or 'none (no feat/fix/perf/breaking commits)'}")
    preview_version = next_version(current, bump) if bump else current
    if bump:
        print(f"Next version: {current} -> {preview_version}")
    print()
    print(format_changelog_entry(preview_version, build_changelog_sections(commits), date.today()))


def cmd_bump(args: argparse.Namespace) -> None:
    ref = latest_tag()
    commits = commits_since(ref)
    bump = args.bump
    if bump == "auto":
        bump = recommend_bump(commits)
        if bump is None:
            print("No feat/fix/perf/breaking commits since the last release - nothing to bump.", file=sys.stderr)
            sys.exit(1)
    current = read_version()
    new_version = next_version(current, bump)
    entry = format_changelog_entry(new_version, build_changelog_sections(commits), date.today())

    if args.dry_run:
        print(f"Would bump {current} -> {new_version} ({bump})\n")
        print(entry)
        return

    write_version(new_version)
    prepend_changelog(entry)
    _run("git", "add", str(VERSION_FILE), str(CHANGELOG_FILE))
    _run("git", "commit", "-m", f"chore(release): v{new_version}")
    _run("git", "tag", "-a", f"v{new_version}", "-m", f"v{new_version}")
    print(f"Bumped {current} -> {new_version} ({bump}), committed, and tagged v{new_version}.")
    print(f"Review with `git show`, then: git push && git push origin v{new_version}")


def cmd_release(args: argparse.Namespace) -> None:
    tag = args.tag or f"v{read_version()}"
    version = tag[1:] if tag.startswith("v") else tag
    section = extract_changelog_section(CHANGELOG_FILE.read_text(encoding="utf-8"), version)
    if section is None:
        print(f"No CHANGELOG.md section found for version {version} - run `bump` first.", file=sys.stderr)
        sys.exit(1)

    cmd = ["gh", "release", "create", tag, "--title", tag, "--notes", section]
    if args.draft:
        cmd.append("--draft")

    if args.dry_run:
        print("Would run:", " ".join(shlex.quote(c) for c in cmd))
        print()
        print(section)
        return

    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_suggest = sub.add_parser("suggest", help="Preview the recommended bump + changelog draft (read-only)")
    p_suggest.set_defaults(func=cmd_suggest)

    p_bump = sub.add_parser("bump", help="Update VERSION + CHANGELOG.md, commit, and tag")
    p_bump.add_argument("bump", choices=["major", "minor", "patch", "auto"])
    p_bump.add_argument("--dry-run", action="store_true", help="Print what would change without doing it")
    p_bump.set_defaults(func=cmd_bump)

    p_release = sub.add_parser("release", help="Create a GitHub release from a CHANGELOG.md section via `gh`")
    p_release.add_argument("--tag", help="Defaults to v<current VERSION>")
    p_release.add_argument("--draft", action="store_true")
    p_release.add_argument("--dry-run", action="store_true")
    p_release.set_defaults(func=cmd_release)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
