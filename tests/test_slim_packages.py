"""deploy/container/slim-packages.sh and the host command's `slim` command and
post-update offer: removing the build tools that installs before 2.0 pulled in
by accident. Runs the real script against a fake apt-get/dpkg-query
(tests/fakes) inside the fake container."""

import shutil

import pytest
from pve_fakes import DEPLOY, FakePve
from test_host_command import _new_release_files, _updatable, _write, stub

SCRIPT = DEPLOY / "container" / "slim-packages.sh"

PLAN = """Reading package lists...
Remv gcc-12 [12.2.0-14]
Remv g++-12 [12.2.0-14]
Remv make [4.3-4.1]
Remv build-essential [12.9]
Remv python3-pip [23.0.1+dfsg-1]
After this operation, 412 MB disk space will be freed.
"""
ALL_ROOTS = "build-essential\npython3-dev\npython3-pip\n"


@pytest.fixture
def pve():
    fake = FakePve()
    yield fake
    fake.close()


def _apt(pve: FakePve, ctid: int = 207, plan: str = PLAN, installed: str = ALL_ROOTS):
    apt = pve.ct(ctid) / "apt"
    _write(apt / "plan", plan)
    _write(apt / "installed", installed)
    return apt


def _put_script(pve: FakePve, ctid: int = 207):
    dest = pve.ct(ctid) / "fs" / "opt" / "pve-flr-portal" / "deploy" / "container" / "slim-packages.sh"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, dest)
    return dest


def _run(pve: FakePve, apt, *args: str, script=SCRIPT):
    return pve.run_script(script, list(args), {"FAKE_ROOT": "1", "FAKE_APT_DIR": apt.as_posix()})


def _purged(apt) -> bool:
    log = (apt / "apt.log").read_text(encoding="utf-8") if (apt / "apt.log").exists() else ""
    return "apt-get purge" in log


# --- the script ----------------------------------------------------------------------------


def test_check_shows_what_would_be_removed_and_changes_nothing(pve):
    apt = _apt(pve)
    result = _run(pve, apt, "check")
    assert result.returncode == 0, result.stderr
    assert "5 packages, about 412 MB" in result.stdout
    assert "gcc-12" in result.stdout and "python3-pip" in result.stdout
    assert not _purged(apt)


def test_nothing_to_remove_is_not_an_error(pve):
    apt = _apt(pve, installed="")
    assert _run(pve, apt, "check").returncode == 3
    assert _run(pve, apt, "apply").returncode == 3
    assert not _purged(apt)


def test_a_plan_that_touches_anything_else_removes_nothing(pve):
    apt = _apt(pve, plan=PLAN + "Remv git [1:2.39.2]\n")
    for mode in ("check", "apply"):
        result = _run(pve, apt, mode)
        assert result.returncode == 1
        assert "not just build tools" in result.stderr
    assert not _purged(apt)


@pytest.mark.parametrize(
    "keep", ["python3", "python3-venv", "python3.11-venv", "ca-certificates", "certbot", "systemd"]
)
def test_the_packages_the_portal_needs_are_never_removed(pve, keep):
    apt = _apt(pve, plan=PLAN + f"Remv {keep} [1]\n")
    assert _run(pve, apt, "apply").returncode == 1
    assert not _purged(apt)


def test_apply_purges_and_cleans_the_package_cache(pve):
    apt = _apt(pve)
    result = _run(pve, apt, "apply")
    assert result.returncode == 0, result.stderr
    log = (apt / "apt.log").read_text(encoding="utf-8")
    assert "purge -y -qq --auto-remove build-essential python3-dev python3-pip" in log
    assert "apt-get clean" in log
    assert "Removed 5 packages" in result.stdout


def test_apply_says_so_if_the_portals_python_environment_broke(pve, tmp_path):
    tree = tmp_path / "app"
    (tree / "deploy" / "container").mkdir(parents=True)
    (tree / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(SCRIPT, tree / "deploy" / "container" / "slim-packages.sh")
    py = tree / ".venv" / "bin" / "python"
    py.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8", newline="\n")
    py.chmod(0o755)
    result = _run(pve, _apt(pve), "apply", script=tree / "deploy" / "container" / "slim-packages.sh")
    assert result.returncode == 1
    assert "no longer imports" in result.stderr


def test_it_needs_root_and_a_known_mode(pve):
    apt = _apt(pve)
    assert _run(pve, apt, "frobnicate").returncode == 2
    result = pve.run_script(SCRIPT, ["check"], {"FAKE_APT_DIR": apt.as_posix()})  # not root
    assert result.returncode == 1 and "root" in result.stderr


# --- the host command ----------------------------------------------------------------------


def test_slim_shows_the_plan_and_with_y_removes_the_packages(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    _put_script(pve)
    apt = _apt(pve)
    result = stub(pve, "slim", "-y")
    assert result.returncode == 0, result.stderr
    assert "5 packages, about 412 MB" in result.stdout
    assert _purged(apt)


def test_slim_without_a_terminal_or_y_removes_nothing(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    _put_script(pve)
    apt = _apt(pve)
    result = stub(pve, "slim")
    assert result.returncode != 0 and "needs -y" in result.stderr
    assert not _purged(apt)


def test_slim_says_when_there_is_nothing_to_do(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    _put_script(pve)
    _apt(pve, installed="")
    assert "nothing to clean up" in stub(pve, "slim", "-y").stdout


def test_slim_refuses_an_unsafe_plan(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    _put_script(pve)
    apt = _apt(pve, plan=PLAN + "Remv python3-venv [3.11]\n")
    result = stub(pve, "slim", "-y")
    assert result.returncode != 0 and "not just build tools" in result.stderr
    assert not _purged(apt)


def test_slim_on_a_release_without_the_clean_up_says_to_update(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    result = stub(pve, "slim", "-y")
    assert result.returncode != 0 and "Update it first" in result.stderr


def _updated_release_has_the_script(pve: FakePve, ctid: int = 207):
    _write(_new_release_files(pve, ctid) / "container" / "slim-packages.sh", SCRIPT.read_text(encoding="utf-8"))


def test_an_update_from_before_2_0_offers_the_clean_up_but_never_does_it_unasked(pve):
    _updatable(pve, version="1.9.0", tags="v1.9.0\nv2.0.0\n")
    _updated_release_has_the_script(pve)
    apt = _apt(pve)
    result = stub(pve, "update", "-y", "--no-snapshot")  # -y answers the update, not the removal
    assert result.returncode == 0, result.stderr
    assert "5 packages, about 412 MB" in result.stdout
    assert "pve-flr-portal slim 207" in result.stdout
    assert not _purged(apt)


def test_an_update_that_is_already_on_2_0_says_nothing_about_it(pve):
    _updatable(pve, version="2.0.0", tags="v2.0.0\nv2.1.0\n")
    _updated_release_has_the_script(pve)
    _apt(pve)
    result = stub(pve, "update", "-y", "--no-snapshot")
    assert result.returncode == 0, result.stderr
    assert "build tools" not in result.stdout


def test_an_update_with_nothing_to_remove_stays_quiet(pve):
    _updatable(pve, version="1.9.0", tags="v1.9.0\nv2.0.0\n")
    _updated_release_has_the_script(pve)
    _apt(pve, installed="")
    result = stub(pve, "update", "-y", "--no-snapshot")
    assert result.returncode == 0, result.stderr
    assert "build tools" not in result.stdout


def test_the_clean_up_scripts_are_executable_in_git():
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "--stage", "deploy/container/slim-packages.sh"],
        cwd=DEPLOY.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.startswith("100755"), out
