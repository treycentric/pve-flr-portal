"""deploy/install-host-tools.sh - installs the `pve-flr-portal` host command on
a host that doesn't have it (or has an old copy), from a container's checkout
or from GitHub. Uses the fake `pct` and `curl` (tests/fakes/)."""

import pytest
from pve_fakes import DEPLOY, FakePve

SCRIPT = DEPLOY / "install-host-tools.sh"
STUB = DEPLOY / "host" / "pve-flr-portal"


@pytest.fixture
def pve():
    fake = FakePve()
    yield fake
    fake.close()


def install(pve: FakePve, *args: str, **env: str):
    sbin = pve.root / "sbin"
    sbin.mkdir(exist_ok=True)
    return pve.run_script(SCRIPT, list(args), {"PFR_SBIN": sbin.as_posix(), **env}), sbin


def _release_has_the_command(pve: FakePve, ctid: int = 207) -> None:
    pve.add_ct(ctid)
    host = pve.ct(ctid) / "fs" / "opt" / "pve-flr-portal" / "deploy" / "host"
    host.mkdir(parents=True)
    (host / "pve-flr-portal").write_bytes(STUB.read_bytes())


def _served(pve: FakePve, name: str, content: bytes) -> str:
    path = pve.root / name
    path.write_bytes(content)
    return path.as_posix()


def test_installs_from_a_containers_checkout(pve):
    _release_has_the_command(pve)
    result, sbin = install(pve, "207")
    assert result.returncode == 0, result.stderr
    assert "Copied from container 207" in result.stdout
    assert (sbin / "pve-flr-portal").read_bytes() == STUB.read_bytes()
    assert (sbin / "pve-flr-portal-config").exists()
    assert (sbin / "pve-flr-portal-update").exists()
    assert "pve-flr-portal adopt" in result.stdout


def test_a_container_whose_release_lacks_the_command_installs_nothing(pve):
    # This is the case that once produced a 0-byte command.
    pve.add_ct(207)
    result, sbin = install(pve, "207")
    assert result.returncode != 0
    assert "doesn't include the host command" in result.stderr
    assert list(sbin.iterdir()) == []


def test_it_rejects_a_container_id_that_is_not_a_number(pve):
    result, sbin = install(pve, "abc")
    assert result.returncode != 0
    assert list(sbin.iterdir()) == []


def test_downloads_the_latest_release_without_a_container(pve):
    tag = _served(pve, "tag.sh", STUB.read_bytes())
    result, sbin = install(
        pve,
        FAKE_LATEST_URL="https://github.com/octo/portal/releases/tag/v1.9.0",
        FAKE_TAG_LIB=tag,
        REPO_URL="https://github.com/octo/portal.git",
    )
    assert result.returncode == 0, result.stderr
    assert "Downloaded from v1.9.0" in result.stdout
    assert (sbin / "pve-flr-portal").read_bytes() == STUB.read_bytes()


def test_falls_back_to_main_when_the_release_predates_the_command(pve):
    main = _served(pve, "main.sh", STUB.read_bytes())
    result, sbin = install(
        pve,
        FAKE_LATEST_URL="https://github.com/octo/portal/releases/tag/v1.9.0",
        FAKE_MAIN_LIB=main,  # the tag has nothing: curl fails for it
        REPO_URL="https://github.com/octo/portal.git",
    )
    assert result.returncode == 0, result.stderr
    assert "Downloaded from main" in result.stdout
    assert (sbin / "pve-flr-portal").exists()


def test_pfr_version_selects_a_branch_or_tag(pve):
    main = _served(pve, "main.sh", STUB.read_bytes())
    result, _ = install(pve, PFR_VERSION="main", FAKE_MAIN_LIB=main, REPO_URL="https://github.com/octo/portal.git")
    assert result.returncode == 0, result.stderr
    assert "Downloaded from main" in result.stdout


@pytest.mark.parametrize(
    "content",
    [
        b"",  # an empty download
        b"<html><body>404: Not Found</body></html>\n",  # an error page
        b"#!/usr/bin/env bash\necho not the command\n",  # a valid script that isn't ours
    ],
)
def test_never_installs_something_that_is_not_the_command(pve, content):
    main = _served(pve, "main.sh", content)
    sbin = pve.root / "sbin"
    sbin.mkdir()
    (sbin / "pve-flr-portal").write_text("#!/usr/bin/env bash\necho keep-me\n", encoding="utf-8", newline="\n")
    result, _ = install(pve, FAKE_MAIN_LIB=main, REPO_URL="https://github.com/octo/portal.git")
    assert result.returncode != 0
    assert "keep-me" in (sbin / "pve-flr-portal").read_text(encoding="utf-8")


def test_replaces_an_existing_copy(pve):
    _release_has_the_command(pve)
    sbin = pve.root / "sbin"
    sbin.mkdir()
    (sbin / "pve-flr-portal").write_bytes(b"")  # the broken 0-byte file from before
    result, _ = install(pve, "207")
    assert result.returncode == 0, result.stderr
    assert (sbin / "pve-flr-portal").read_bytes() == STUB.read_bytes()


def test_leaves_no_temp_files_behind(pve):
    _release_has_the_command(pve)
    install(pve, "207")
    install(pve, "999")  # a failure
    assert list(pve.tmpdir.iterdir()) == []


def test_the_script_is_executable_in_git():
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "--stage", "deploy/install-host-tools.sh"],
        cwd=DEPLOY.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.startswith("100755"), out
