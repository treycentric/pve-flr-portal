"""Tests for scripts/release.py's git/gh orchestration (`bump --pr`, `tag`).

Every `_run` / `subprocess.run` call is stubbed - these assert on *which*
git commands the flow issues, without a real repo or network. The pure
logic is covered in test_release.py.
"""
import argparse

import pytest

import scripts.release as release


class _FakeGit:
    """Records `_run(*args)` calls and answers the few queries the flow makes."""

    def __init__(self, **answers):
        self.calls: list[tuple[str, ...]] = []
        self.answers = answers

    def __call__(self, *args):
        self.calls.append(args)
        for key, value in self.answers.items():
            if key in args or (len(args) >= 2 and args[1] == key):
                return value
        return ""

    def issued(self, *prefix):
        return any(call[: len(prefix)] == prefix for call in self.calls)


@pytest.fixture
def stub_pure(monkeypatch):
    monkeypatch.setattr(release, "latest_tag", lambda: "v1.0.0")
    monkeypatch.setattr(
        release, "commits_since", lambda ref: [release.Commit("abc1234def", "feat: a thing (#1)")]
    )
    monkeypatch.setattr(release, "read_version", lambda *a, **k: "1.0.0")
    monkeypatch.setattr(release, "write_version", lambda *a, **k: None)
    monkeypatch.setattr(release, "prepend_changelog", lambda *a, **k: None)


def test_bump_pr_branches_off_origin_commits_pushes_and_opens_pr(monkeypatch, stub_pure):
    git = _FakeGit(**{"--abbrev-ref": "main\n", "--porcelain": "", "--list": ""})
    monkeypatch.setattr(release, "_run", git)
    monkeypatch.setattr(release.shutil, "which", lambda name: "/usr/bin/gh")

    gh_calls = []
    monkeypatch.setattr(
        release.subprocess, "run", lambda cmd, **kw: gh_calls.append(cmd) or type("R", (), {"returncode": 0})()
    )

    ns = argparse.Namespace(bump="auto", pr=True, base="main", dry_run=False)
    release.cmd_bump(ns)

    assert git.issued("git", "fetch", "origin", "main")
    assert git.issued("git", "switch", "-c", "release/v1.1.0", "origin/main")
    assert git.issued("git", "commit", "-m", "chore(release): v1.1.0")
    assert git.issued("git", "push", "-u", "origin", "release/v1.1.0")
    assert git.issued("git", "switch", "main")  # switched back
    assert not git.issued("git", "tag")  # never tags in the --pr flow
    assert gh_calls and gh_calls[0][:3] == ["gh", "pr", "create"]


def test_bump_pr_refuses_a_dirty_worktree(monkeypatch, stub_pure):
    git = _FakeGit(**{"--porcelain": " M somefile\n", "--list": ""})
    monkeypatch.setattr(release, "_run", git)
    monkeypatch.setattr(release.shutil, "which", lambda name: "/usr/bin/gh")

    ns = argparse.Namespace(bump="auto", pr=True, base="main", dry_run=False)
    with pytest.raises(SystemExit):
        release.cmd_bump(ns)
    assert not git.issued("git", "switch", "-c", "release/v1.1.0", "origin/main")


def test_bump_pr_needs_gh(monkeypatch, stub_pure):
    monkeypatch.setattr(release, "_run", _FakeGit())
    monkeypatch.setattr(release.shutil, "which", lambda name: None)
    ns = argparse.Namespace(bump="auto", pr=True, base="main", dry_run=False)
    with pytest.raises(SystemExit):
        release.cmd_bump(ns)


def test_tag_tags_and_pushes_when_head_is_the_release_commit(monkeypatch):
    monkeypatch.setattr(release, "read_version", lambda *a, **k: "1.1.0")
    git = _FakeGit(
        **{
            "--porcelain": "",
            "--pretty=%s": "chore(release): v1.1.0 (#42)\n",
            "--list": "",
            "--tags": "",
            "--short": "deadbee\n",
        }
    )
    monkeypatch.setattr(release, "_run", git)

    release.cmd_tag(argparse.Namespace(base="main", dry_run=False))

    assert git.issued("git", "merge", "--ff-only", "origin/main")
    assert git.issued("git", "tag", "-a", "v1.1.0", "-m", "v1.1.0")
    assert git.issued("git", "push", "origin", "v1.1.0")


def test_tag_refuses_when_head_is_not_the_release_commit(monkeypatch):
    monkeypatch.setattr(release, "read_version", lambda *a, **k: "1.1.0")
    git = _FakeGit(**{"--porcelain": "", "--pretty=%s": "feat: unrelated work\n"})
    monkeypatch.setattr(release, "_run", git)

    with pytest.raises(SystemExit):
        release.cmd_tag(argparse.Namespace(base="main", dry_run=False))
    assert not git.issued("git", "tag", "-a", "v1.1.0", "-m", "v1.1.0")


def test_tag_refuses_when_tag_already_exists(monkeypatch):
    monkeypatch.setattr(release, "read_version", lambda *a, **k: "1.1.0")
    git = _FakeGit(
        **{"--porcelain": "", "--pretty=%s": "chore(release): v1.1.0\n", "--list": "v1.1.0\n"}
    )
    monkeypatch.setattr(release, "_run", git)

    with pytest.raises(SystemExit):
        release.cmd_tag(argparse.Namespace(base="main", dry_run=False))
    assert not git.issued("git", "push", "origin", "v1.1.0")
