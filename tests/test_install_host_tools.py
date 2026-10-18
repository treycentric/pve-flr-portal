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


def _raw_with_stub_and_installer(pve: FakePve, installer: bool = True):
    raw = pve.root / "raw"
    (raw / "v1.9.0").mkdir(parents=True)
    (raw / "v1.9.0" / "pve-flr-portal").write_bytes(STUB.read_bytes())
    if installer:
        (raw / "v1.9.0" / "lxc-create.sh").write_text(
            '#!/usr/bin/env bash\nPFR_LIB_API_WANTED=1\necho "INSTALLER-RAN"\n', encoding="utf-8", newline="\n"
        )
    return raw


def _github(raw) -> dict[str, str]:
    return {
        "FAKE_RAW_DIR": raw.as_posix(),
        "FAKE_LATEST_URL": "https://github.com/octo/portal/releases/tag/v1.9.0",
        "REPO_URL": "https://github.com/octo/portal.git",
    }


def test_install_flag_installs_the_command_and_then_runs_the_installer(pve):
    raw = _raw_with_stub_and_installer(pve)
    result, sbin = install(pve, "--install", **_github(raw))
    assert result.returncode == 0, result.stderr
    assert (sbin / "pve-flr-portal").exists()
    assert "INSTALLER-RAN" in result.stdout
    assert list(pve.tmpdir.iterdir()) == []  # the exec skips the exit trap, so it cleaned up first


def test_without_the_install_flag_it_only_installs_the_command(pve):
    raw = _raw_with_stub_and_installer(pve)
    result, _ = install(pve, **_github(raw))
    assert result.returncode == 0, result.stderr
    assert "INSTALLER-RAN" not in result.stdout
    assert "pve-flr-portal install" in result.stdout  # points at the next step


# --- the bootstrap installs the roles tool alongside the command ----------------------------


def _roles_script_text() -> str:
    return (DEPLOY / "host" / "pfr-roles.sh").read_text(encoding="utf-8")


def test_the_bootstrap_installs_the_roles_tool_from_github(pve):
    raw = _raw_with_stub_and_installer(pve, installer=False)
    (raw / "v1.9.0" / "pfr-roles.sh").write_text(_roles_script_text(), encoding="utf-8", newline="\n")
    result, sbin = install(pve, **_github(raw))
    assert result.returncode == 0, result.stderr
    assert "PFR_ROLES_SCRIPT=1" in (sbin / "pve-flr-portal-roles").read_text(encoding="utf-8")
    assert "pve-flr-portal roles" in result.stdout


def test_the_bootstrap_installs_the_roles_tool_from_a_containers_checkout(pve):
    _release_has_the_command(pve)
    host = pve.ct(207) / "fs" / "opt" / "pve-flr-portal" / "deploy" / "host"
    (host / "pfr-roles.sh").write_text(_roles_script_text(), encoding="utf-8", newline="\n")
    result, sbin = install(pve, "207")
    assert result.returncode == 0, result.stderr
    assert (sbin / "pve-flr-portal-roles").exists()


def test_a_missing_or_foreign_roles_tool_does_not_stop_the_command_being_installed(pve):
    raw = _raw_with_stub_and_installer(pve, installer=False)  # no roles tool in this release
    result, sbin = install(pve, **_github(raw))
    assert result.returncode == 0, result.stderr
    assert (sbin / "pve-flr-portal").exists()
    assert not (sbin / "pve-flr-portal-roles").exists()

    (raw / "v1.9.0" / "pfr-roles.sh").write_text(
        "#!/usr/bin/env bash\necho not-the-tool\n", encoding="utf-8", newline="\n"
    )
    result, sbin = install(pve, **_github(raw))
    assert result.returncode == 0, result.stderr
    assert not (sbin / "pve-flr-portal-roles").exists()
