"""The deploy/ tree changed layout (scripts that run on the PVE node under
host/, those that run inside the container under container/). A host runs
against releases of either layout, and a container on either layout is
managed from the same host, so both directions are covered here:

- forward: the current host tools against a release with the flat layout
  (container checkouts, GitHub refs);
- backward: what earlier releases and existing installs reach by the old
  paths still resolves (shims, the service template, certbot renewal hooks);
- the container image is independent of deploy/.

Uses the same fakes as the other deploy tests (tests/fakes)."""

import os
import re
import subprocess
from pathlib import Path

import pytest
from pve_fakes import DEPLOY, ROOT, FakePve
from test_deploy import BASH
from test_host_command import (
    NEW_CMD,
    _github_env,
    _new_release_files,
    _updatable,
    _write,
    stub,
)

# What an earlier release or install still reaches by its old, flat path.
OLD_PATHS = ["update.sh", "lxc-create.sh", "pve-flr-portal.service.template"]


@pytest.fixture
def pve():
    fake = FakePve()
    yield fake
    fake.close()


def _container_deploy(pve: FakePve, ctid: int = 207) -> Path:
    return pve.ct(ctid) / "fs" / "opt" / "pve-flr-portal" / "deploy"


# --- forward: the host tools against a flat-layout release --------------------------------


def test_update_runs_the_flat_update_script_of_an_old_layout_release(pve):
    _updatable(pve, layout="old")
    result = stub(pve, "update", "-y", "--no-snapshot")
    assert result.returncode == 0, result.stderr
    log = pve.log("exec.log")
    assert "bash /opt/pve-flr-portal/deploy/update.sh" in log
    assert "deploy/container/update.sh" not in log


def test_update_runs_the_new_update_script_of_a_new_layout_release(pve):
    _updatable(pve, layout="new")
    assert stub(pve, "update", "-y", "--no-snapshot").returncode == 0
    assert "bash /opt/pve-flr-portal/deploy/container/update.sh" in pve.log("exec.log")


def test_the_new_location_wins_when_a_release_has_both(pve):
    # a current release carries the old path as a forwarding shim
    _updatable(pve, layout="new")
    _write(_container_deploy(pve) / "update.sh", "#!/usr/bin/env bash\nexec true\n")
    assert stub(pve, "update", "-y", "--no-snapshot").returncode == 0
    log = pve.log("exec.log")
    assert "deploy/container/update.sh" in log
    assert "bash /opt/pve-flr-portal/deploy/update.sh" not in log


@pytest.mark.parametrize(("layout", "expect"), [("old", "deploy/pfr-manage.sh"), ("new", "deploy/host/pfr-manage.sh")])
def test_config_finds_the_management_tools_of_either_layout(pve, layout, expect):
    pve.add_ct(207, tags="pve-flr-portal", layout=layout)
    pve.install_tools(207, layout=layout)
    stub(pve, "config")  # runs the pulled tool, which refuses without a terminal
    assert f"pct pull 207 /opt/pve-flr-portal/{expect}" in pve.log("calls.log")


def test_a_legacy_install_picks_whichever_install_script_the_release_has(pve, tmp_path):
    _updatable(pve, version="1.0.0", update_script=False, git_checkout=True, layout="new")
    _write(_new_release_files(pve) / "container" / "update.sh", "#!/usr/bin/env bash\n")
    assert stub(pve, "update", "-y", "--no-snapshot").returncode == 0
    log = pve.log("exec.log")
    script = log[log.index("bash -c") :]
    picks = [line for line in script.splitlines() if line.startswith("INSTALL=") or line.startswith('[ -f "$INSTALL"')]
    assert picks, script
    for present, expected in (("deploy/container/install.sh", "container"), ("deploy/install.sh", "flat")):
        work = tmp_path / expected
        (work / present).parent.mkdir(parents=True)
        (work / present).write_text("", encoding="utf-8")
        out = subprocess.run(
            [BASH, "-c", "\n".join(picks) + '\necho "$INSTALL"'], cwd=work, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert out == present


PATCH_PATHS = ["patch-pve-host-backups.sh", "host/patch-pve-host-backups.sh"]


@pytest.mark.parametrize("path", PATCH_PATHS)
def test_the_patch_script_is_refreshed_from_either_layout(pve, path):
    _updatable(pve)
    sbin = pve.root / "sbin"
    _write(sbin / "pve-flr-portal-patch-host-backups.sh", "#!/usr/bin/env bash\necho old\n")
    _write(_new_release_files(pve) / path, "#!/usr/bin/env bash\necho refreshed\n")
    assert stub(pve, "update", "-y", "--no-snapshot").returncode == 0
    assert "refreshed" in (sbin / "pve-flr-portal-patch-host-backups.sh").read_text(encoding="utf-8")


def test_a_mixed_fleet_is_updated_with_each_containers_own_layout(pve):
    _updatable(pve, 207, layout="new")
    _updatable(pve, 208, layout="old")
    result = stub(pve, "update", "--all", "-y", "--no-snapshot")
    assert result.returncode == 0, result.stderr
    log = pve.log("exec.log")
    assert "deploy/container/update.sh" in log
    assert "bash /opt/pve-flr-portal/deploy/update.sh" in log
    assert pve.read(207, "/opt/pve-flr-portal/VERSION").strip() == "1.10.0"
    assert pve.read(208, "/opt/pve-flr-portal/VERSION").strip() == "1.10.0"


# --- forward: GitHub refs of either layout ----------------------------------------------------


def _strict(env: dict[str, str]) -> dict[str, str]:
    return {**env, "FAKE_RAW_STRICT": "1"}


def test_install_takes_the_installer_from_the_new_location(pve):
    raw = pve.root / "raw"
    _write(
        raw / "v1.9.0" / "deploy" / "host" / "lxc-create.sh", "#!/usr/bin/env bash\nPFR_LIB_API_WANTED=1\necho NEW\n"
    )
    _write(raw / "v1.9.0" / "deploy" / "lxc-create.sh", "#!/usr/bin/env bash\nPFR_LIB_API_WANTED=1\necho FLAT\n")
    result = stub(pve, "install", env=_strict(_github_env(pve, raw)))
    assert result.returncode == 0, result.stderr
    assert "NEW" in result.stdout and "FLAT" not in result.stdout


def test_install_falls_back_to_the_installer_at_the_old_location(pve):
    raw = pve.root / "raw"
    _write(raw / "v1.9.0" / "deploy" / "lxc-create.sh", "#!/usr/bin/env bash\nPFR_LIB_API_WANTED=1\necho FLAT\n")
    result = stub(pve, "install", env=_strict(_github_env(pve, raw)))
    assert result.returncode == 0, result.stderr
    assert "FLAT" in result.stdout


@pytest.mark.parametrize("path", PATCH_PATHS)
def test_host_tools_from_github_refreshes_the_patch_script_from_either_layout(pve, path):
    raw = pve.root / "raw"
    _write(raw / "v1.9.0" / "deploy" / "host" / "pve-flr-portal", NEW_CMD)
    _write(raw / "v1.9.0" / "deploy" / path, "#!/usr/bin/env bash\necho new-patch\n")
    sbin = pve.root / "sbin"
    _write(sbin / "pve-flr-portal-patch-host-backups.sh", "#!/usr/bin/env bash\necho old-patch\n")
    stub(pve, "host-tools", env=_strict(_github_env(pve, raw)))
    assert "new-patch" in (sbin / "pve-flr-portal-patch-host-backups.sh").read_text(encoding="utf-8")


# --- forward: the installer inside a container of either layout -----------------------------


def _lib(pve: FakePve, body: str):
    lib = (DEPLOY / "host" / "lib" / "settings.sh").as_posix()
    return pve.bash(f'set -uo pipefail\nsource "{lib}"\n{body}\n')


def test_the_installer_finds_the_container_scripts_in_either_layout(pve):
    pve.add_ct(207, layout="new")
    pve.add_ct(208, layout="old")
    deploy_new, deploy_old = _container_deploy(pve, 207), _container_deploy(pve, 208)
    for name in ("install.sh", "certbot-setup.sh"):
        _write(deploy_new / "container" / name, "#!/usr/bin/env bash\n")
        _write(deploy_old / name, "#!/usr/bin/env bash\n")
    out = _lib(
        pve,
        "pfr_release_path 207 container/install.sh; pfr_release_path 208 container/install.sh;"
        " pfr_release_path 207 container/certbot-setup.sh; pfr_release_path 208 container/certbot-setup.sh;"
        ' pfr_release_path 208 container/missing.sh || echo "none"',
    ).stdout.split()
    assert out == [
        "/opt/pve-flr-portal/deploy/container/install.sh",
        "/opt/pve-flr-portal/deploy/install.sh",
        "/opt/pve-flr-portal/deploy/container/certbot-setup.sh",
        "/opt/pve-flr-portal/deploy/certbot-setup.sh",
        "none",
    ]


def test_the_installer_runs_the_install_script_of_the_release_it_cloned(pve):
    text = (DEPLOY / "host" / "lxc-create.sh").read_text(encoding="utf-8")
    block = re.search(r"^INSTALL_SCRIPT=.*?^msg_ok \"Installed pve-flr-portal\"\n", text, re.S | re.M).group(0)
    for ctid, layout, expect in ((207, "new", "deploy/container/install.sh"), (208, "old", "deploy/install.sh")):
        pve.add_ct(ctid, layout=layout)
        _write(_container_deploy(pve, ctid) / ("container/install.sh" if layout == "new" else "install.sh"), "")
        result = _lib(
            pve,
            f'msg_err() {{ echo "ERR: $*" >&2; }}; msg_ok() {{ :; }}\n'
            f"CTID={ctid} INSTALL_CERTBOT=0 CERTBOT_DNS_PLUGIN=rfc2136\n{block}",
        )
        assert result.returncode == 0, result.stderr
        assert f"bash /opt/pve-flr-portal/{expect}" in pve.log("exec.log")


# --- backward: old paths keep working in a current release ----------------------------------


def test_every_path_an_earlier_release_or_install_uses_still_exists():
    missing = [p for p in OLD_PATHS if not (DEPLOY / p).exists()]
    assert not missing, f"old deploy paths that no longer resolve: {missing}"


def test_the_old_service_template_matches_the_new_one():
    # an update that is already running from an earlier release regenerates
    # the unit from the old path after it checks out the new release
    old = (DEPLOY / "pve-flr-portal.service.template").read_bytes().replace(b"\r\n", b"\n")
    new = (DEPLOY / "container" / "pve-flr-portal.service.template").read_bytes().replace(b"\r\n", b"\n")
    assert old == new


@pytest.mark.parametrize("name", ["update.sh"])
def test_a_container_script_shim_forwards_its_arguments_and_environment(name, tmp_path):
    tree = tmp_path / "deploy"
    (tree / "container").mkdir(parents=True)
    (tree / name).write_bytes((DEPLOY / name).read_bytes().replace(b"\r\n", b"\n"))
    (tree / "container" / name).write_text(
        '#!/usr/bin/env bash\necho "ran container/$(basename "$0") args=[$*] lineage=[${RENEWED_LINEAGE:-}]"\n',
        encoding="utf-8",
        newline="\n",
    )
    result = subprocess.run(
        [BASH, (tree / name).as_posix(), "v1.4.0", "-y"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "RENEWED_LINEAGE": "/etc/letsencrypt/live/x"},
    )
    assert result.returncode == 0, result.stderr
    assert f"ran container/{name} args=[v1.4.0 -y] lineage=[/etc/letsencrypt/live/x]" in result.stdout


def test_a_shim_works_when_reached_through_a_symlink_free_relative_path(tmp_path):
    tree = tmp_path / "deploy"
    (tree / "container").mkdir(parents=True)
    (tree / "update.sh").write_bytes((DEPLOY / "update.sh").read_bytes().replace(b"\r\n", b"\n"))
    (tree / "container" / "update.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8", newline="\n")
    result = subprocess.run([BASH, "deploy/update.sh"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0 and "ok" in result.stdout


def test_the_container_scripts_still_find_the_app_directory_from_their_new_place():
    for name in ("install.sh", "update.sh"):
        text = (DEPLOY / "container" / name).read_text(encoding="utf-8")
        assert 'APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"' in text, name


def test_the_old_installer_path_runs_the_new_installer_from_a_checkout(pve, tmp_path):
    tree = tmp_path / "deploy"
    (tree / "host").mkdir(parents=True)
    (tree / "lxc-create.sh").write_bytes((DEPLOY / "lxc-create.sh").read_bytes().replace(b"\r\n", b"\n"))
    (tree / "host" / "lxc-create.sh").write_text(
        '#!/usr/bin/env bash\necho "NEW-INSTALLER args=[$*]"\n', encoding="utf-8", newline="\n"
    )
    result = pve.run_script(tree / "lxc-create.sh", ["--x"])
    assert result.returncode == 0, result.stderr
    assert "NEW-INSTALLER args=[--x]" in result.stdout


def test_the_old_installer_one_liner_downloads_the_new_installer(pve, tmp_path):
    # `bash -c "$(curl .../deploy/lxc-create.sh)"`: no checkout beside it
    shim = tmp_path / "shim.sh"
    shim.write_bytes((DEPLOY / "lxc-create.sh").read_bytes().replace(b"\r\n", b"\n"))
    raw = pve.root / "raw"
    _write(
        raw / "main" / "deploy" / "host" / "lxc-create.sh",
        "#!/usr/bin/env bash\nPFR_LIB_API_WANTED=1\necho DOWNLOADED\n",
    )
    result = pve.run_script(shim, [], {"FAKE_RAW_DIR": raw.as_posix(), "FAKE_RAW_STRICT": "1"})
    assert result.returncode == 0, result.stderr
    assert "DOWNLOADED" in result.stdout


def test_the_old_installer_one_liner_refuses_something_that_is_not_the_installer(pve, tmp_path):
    shim = tmp_path / "shim.sh"
    shim.write_bytes((DEPLOY / "lxc-create.sh").read_bytes().replace(b"\r\n", b"\n"))
    raw = pve.root / "raw"
    _write(raw / "main" / "deploy" / "host" / "lxc-create.sh", "#!/usr/bin/env bash\necho NOT-THE-INSTALLER\n")
    result = pve.run_script(shim, [], {"FAKE_RAW_DIR": raw.as_posix(), "FAKE_RAW_STRICT": "1"})
    assert result.returncode != 0
    assert "NOT-THE-INSTALLER" not in result.stdout
    assert "Could not download the installer" in result.stderr


@pytest.mark.parametrize("path", ["host/lib/settings.sh", "lib/settings.sh"])
def test_the_installer_gets_its_library_from_either_layout_on_github(pve, path):
    script = (DEPLOY / "host" / "lxc-create.sh").read_text(encoding="utf-8")
    load_lib = re.search(r"^load_lib\(\) \{\n.*?^\}\n", script, re.S | re.M).group(0)
    raw = pve.root / "raw"
    _write(raw / "main" / "deploy" / path, f"PFR_LIB_API=1\nPFR_FROM={path}\n")
    runner = pve.root / "run.sh"
    runner.write_text(
        "set -euo pipefail\n"
        'msg_err() { echo "ERR: $*" >&2; }\nmsg_info() { :; }\n'
        'REPO_URL="https://github.com/octo/portal.git"\nPFR_LIB_API_WANTED=1\n'
        + load_lib
        + 'load_lib\necho "from=$PFR_FROM"\n',
        encoding="utf-8",
        newline="\n",
    )
    result = pve.run_script(runner, [], {"FAKE_RAW_DIR": raw.as_posix(), "FAKE_RAW_STRICT": "1"})
    assert f"from={path}" in result.stdout, result.stderr


# --- the container image does not depend on deploy/ ------------------------------------------


def test_the_container_image_neither_ships_nor_uses_the_deploy_tree():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "deploy/" in ignore, "the image build context would include the deploy scripts"
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    code = [line for line in dockerfile.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert not any("deploy" in line for line in code), "the Dockerfile reaches into deploy/"
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "deploy/" not in "\n".join(line for line in compose.splitlines() if not line.lstrip().startswith("#"))
    for source in [*(ROOT / "backend").glob("*.py"), ROOT / "run.py"]:
        text = source.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+deploy\b", text, re.M), source.name
        assert not re.search(r"[\"']deploy/", text), f"{source.name} reads a file under deploy/"
