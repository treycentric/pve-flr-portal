"""backend/version.py is the single source of truth the About box reads
(index.html's app_version/repo_url) - see docs/dev/versioning.md."""


def test_version_matches_the_real_repo_version_file(project_root):
    from backend import version

    assert version.__version__ == (project_root / "VERSION").read_text().strip()


def test_repo_url_points_at_the_real_github_repo():
    from backend import version

    assert version.REPO_URL == "https://github.com/treycentric/pve-flr-portal"


def test_falls_back_when_version_file_is_missing(monkeypatch, tmp_path):
    from backend import version

    monkeypatch.setattr(version, "_VERSION_FILE", tmp_path / "does-not-exist")
    assert version._read_version() == "0.0.0-unknown"


def test_reads_and_strips_whitespace(monkeypatch, tmp_path):
    from backend import version

    f = tmp_path / "VERSION"
    f.write_text("1.2.3\n")
    monkeypatch.setattr(version, "_VERSION_FILE", f)
    assert version._read_version() == "1.2.3"
