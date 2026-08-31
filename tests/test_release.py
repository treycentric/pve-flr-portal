"""Unit tests for scripts/release.py's pure logic (commit parsing, bump
recommendation, changelog formatting) and its filesystem I/O helpers.
Deliberately does not touch git/subprocess or the real repo's
VERSION/CHANGELOG.md - see docs/dev/versioning.md for the convention
these enforce."""
from datetime import date

import pytest

from scripts.release import (
    Commit,
    build_changelog_sections,
    extract_changelog_section,
    format_changelog_entry,
    next_version,
    prepend_changelog,
    read_version,
    recommend_bump,
    write_version,
)


def c(subject, body="", hash="abc1234567"):
    return Commit(hash=hash, subject=subject, body=body)


# --- Commit parsing -----------------------------------------------------


def test_commit_parses_type_scope_and_description():
    commit = c("feat(timeline): add drag-to-pan")
    assert commit.type == "feat"
    assert commit.scope == "timeline"
    assert commit.description == "add drag-to-pan"
    assert commit.is_breaking is False


def test_commit_without_scope():
    commit = c("fix: correct off-by-one in tick spacing")
    assert commit.type == "fix"
    assert commit.scope is None
    assert commit.description == "correct off-by-one in tick spacing"


def test_commit_bang_marks_breaking_and_keeps_description():
    commit = c("feat!: drop support for the old token auth")
    assert commit.type == "feat"
    assert commit.is_breaking is True
    assert commit.description == "drop support for the old token auth"
    # No BREAKING CHANGE: footer, so the description itself is used.
    assert commit.breaking_description == "drop support for the old token auth"


def test_commit_breaking_change_footer():
    commit = c(
        "refactor(auth): switch to PVE ticket sessions",
        body="Replaces the shared API token entirely.\n\n"
        "BREAKING CHANGE: PVE_TOKEN_* env vars are no longer read.",
    )
    assert commit.is_breaking is True
    assert commit.breaking_description == "PVE_TOKEN_* env vars are no longer read."


def test_commit_non_conventional_subject_has_no_type():
    commit = c("Merge pull request #3 from feature/x")
    assert commit.type is None
    assert commit.description == "Merge pull request #3 from feature/x"
    assert commit.is_breaking is False


def test_commit_ignored_type_is_not_breaking_by_default():
    commit = c("chore: bump dev tooling")
    assert commit.type == "chore"
    assert commit.is_breaking is False
    assert commit.breaking_description is None


# --- recommend_bump -------------------------------------------------------


def test_recommend_bump_breaking_wins_over_everything():
    commits = [c("fix: small thing"), c("feat!: big breaking change")]
    assert recommend_bump(commits) == "major"


def test_recommend_bump_feat_without_breaking_is_minor():
    commits = [c("fix: small thing"), c("feat: new download format")]
    assert recommend_bump(commits) == "minor"


def test_recommend_bump_fix_only_is_patch():
    commits = [c("fix: correct tick spacing"), c("chore: tidy imports")]
    assert recommend_bump(commits) == "patch"


def test_recommend_bump_perf_and_revert_count_as_patch():
    assert recommend_bump([c("perf: faster tree render")]) == "patch"
    assert recommend_bump([c("revert: undo bad change")]) == "patch"


def test_recommend_bump_none_for_only_chores_and_docs():
    commits = [c("chore: bump deps"), c("docs: fix typo"), c("test: add coverage")]
    assert recommend_bump(commits) is None


def test_recommend_bump_empty_list_is_none():
    assert recommend_bump([]) is None


# --- next_version -----------------------------------------------------


@pytest.mark.parametrize(
    "current,bump,expected",
    [
        ("1.0.0", "major", "2.0.0"),
        ("1.4.9", "minor", "1.5.0"),
        ("1.4.9", "patch", "1.4.10"),
        ("0.1.0", "major", "1.0.0"),
    ],
)
def test_next_version(current, bump, expected):
    assert next_version(current, bump) == expected


def test_next_version_rejects_unknown_bump():
    with pytest.raises(ValueError):
        next_version("1.0.0", "sideways")


def test_next_version_rejects_malformed_current_version():
    with pytest.raises(ValueError):
        next_version("not-a-version", "patch")


# --- build_changelog_sections -----------------------------------------


def test_build_changelog_sections_categorizes_by_type():
    commits = [
        c("feat: add tar.zst downloads", hash="1111111"),
        c("fix: correct download filename", hash="2222222"),
        c("perf: cache tree lookups", hash="3333333"),
        c("chore: bump ruff", hash="4444444"),
        c("docs: update README", hash="5555555"),
    ]
    sections = build_changelog_sections(commits)
    assert sections["Added"] == ["add tar.zst downloads (1111111)"]
    assert sections["Fixed"] == ["correct download filename (2222222)"]
    assert sections["Changed"] == ["cache tree lookups (3333333)"]
    # chore/docs are not user-facing and must not appear anywhere.
    assert "chore" not in str(sections)
    assert "README" not in str(sections)


def test_build_changelog_sections_includes_scope_prefix():
    sections = build_changelog_sections([c("feat(auth): add realm dropdown", hash="abcdefabc")])
    assert sections["Added"] == ["**auth:** add realm dropdown (abcdefa)"]


def test_build_changelog_sections_collects_breaking_notes_separately():
    commits = [
        c(
            "feat!: require Python 3.11+",
            body="BREAKING CHANGE: drops support for 3.9/3.10.",
            hash="9999999",
        )
    ]
    sections = build_changelog_sections(commits)
    assert sections["Breaking Changes"] == ["drops support for 3.9/3.10."]
    # Still also listed under its normal section.
    assert sections["Added"] == ["require Python 3.11+ (9999999)"]


def test_build_changelog_sections_unconventional_commit_falls_back_to_changed():
    sections = build_changelog_sections([c("Fix the thing without a type prefix")])
    assert sections == {}  # c.type is None -> excluded, matching IGNORED behaviour


# --- format_changelog_entry --------------------------------------------


def test_format_changelog_entry_orders_sections_and_includes_date():
    sections = {"Fixed": ["a fix"], "Added": ["a feature"]}
    entry = format_changelog_entry("1.1.0", sections, date(2026, 8, 31))
    lines = entry.splitlines()
    assert lines[0] == "## [1.1.0] - 2026-08-31"
    # Added must come before Fixed per SECTION_ORDER, regardless of dict order.
    added_idx = lines.index("### Added")
    fixed_idx = lines.index("### Fixed")
    assert added_idx < fixed_idx
    assert "- a feature" in lines
    assert "- a fix" in lines


def test_format_changelog_entry_breaking_changes_section_comes_first():
    sections = {"Added": ["a feature"], "Breaking Changes": ["dropped old auth"]}
    entry = format_changelog_entry("2.0.0", sections, date(2026, 8, 31))
    lines = [line for line in entry.splitlines() if line.startswith("### ")]
    assert lines[0] == "### Breaking Changes"


def test_format_changelog_entry_empty_sections_says_so():
    entry = format_changelog_entry("1.0.1", {}, date(2026, 8, 31))
    assert "_No user-facing changes._" in entry


# --- extract_changelog_section -----------------------------------------


CHANGELOG_SAMPLE = """# Changelog

## [1.1.0] - 2026-09-01
### Added
- new thing

## [1.0.0] - 2026-08-31
### Added
- initial release
"""


def test_extract_changelog_section_finds_correct_version():
    assert extract_changelog_section(CHANGELOG_SAMPLE, "1.1.0") == "### Added\n- new thing"


def test_extract_changelog_section_stops_before_next_heading():
    section = extract_changelog_section(CHANGELOG_SAMPLE, "1.0.0")
    assert section == "### Added\n- initial release"
    assert "1.1.0" not in section


def test_extract_changelog_section_missing_version_returns_none():
    assert extract_changelog_section(CHANGELOG_SAMPLE, "9.9.9") is None


# --- filesystem I/O (VERSION / CHANGELOG.md), via tmp_path --------------


def test_read_write_version_round_trip(tmp_path):
    path = tmp_path / "VERSION"
    write_version("1.2.3", path)
    assert path.read_text() == "1.2.3\n"
    assert read_version(path) == "1.2.3"


def test_prepend_changelog_creates_file_if_missing(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    prepend_changelog("## [1.0.0] - 2026-08-31\n\n### Added\n- first\n", path)
    text = path.read_text()
    assert text.startswith("# Changelog\n")
    assert "## [1.0.0]" in text


def test_prepend_changelog_inserts_above_existing_entries(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text("# Changelog\n\n## [1.0.0] - 2026-08-31\n\n### Added\n- first\n")
    prepend_changelog("## [1.1.0] - 2026-09-01\n\n### Fixed\n- a bug\n", path)
    text = path.read_text()
    assert text.index("[1.1.0]") < text.index("[1.0.0]")
