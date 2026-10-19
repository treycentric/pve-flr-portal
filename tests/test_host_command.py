"""deploy/host/pve-flr-portal - the host command: finding the containers it
manages by tag, choosing one, adopting existing installs, and handing off to
the management tools in the container's own checkout. Run against a fake
`pct` (tests/fakes/pct)."""

import shutil
from pathlib import Path

import pytest
from pve_fakes import DEPLOY, FakePve

STUB = DEPLOY / "host" / "pve-flr-portal"


@pytest.fixture
def pve():
    fake = FakePve()
    yield fake
    fake.close()


def stub(
    pve: FakePve,
    *args: str,
    stdin: str | None = None,
    name: str = "pve-flr-portal",
    env: dict[str, str] | None = None,
):
    script = pve.root / name
    if not script.exists():
        shutil.copy(STUB, script)
    sbin = pve.root / "sbin"
    sbin.mkdir(exist_ok=True)
    return pve.run_script(script, list(args), {"PFR_SBIN": sbin.as_posix(), **(env or {})}, stdin)


def test_list_shows_only_tagged_containers_with_their_versions(pve):
    pve.add_ct(207, name="flr-prod", tags="pve-flr-portal", version="1.9.0")
    pve.add_ct(208, name="other", tags="web;pve-flr-portal;prod", version="1.8.2")
    pve.add_ct(209, name="unrelated", tags="web")
    pve.add_ct(210, name="lookalike", tags="pve-flr-portal-staging")
    pve.add_ct(211, name="stopped", tags="pve-flr-portal", status="stopped")
    (pve.ct(208) / "lock").write_text("backup\n", encoding="utf-8", newline="\n")  # a lock column shifts the fields
    out = stub(pve, "list").stdout
    assert "207" in out and "flr-prod" in out and "1.9.0" in out
    assert "208" in out and "other" in out and "1.8.2" in out
    assert "211" in out and "stopped" in out
    assert "209" not in out and "210" not in out


def test_status_reports_version_service_and_address(pve):
    pve.add_ct(207, name="flr-prod", tags="pve-flr-portal", version="1.9.0")
    out = stub(pve, "status").stdout  # one container: no question
    assert "207 (flr-prod)" in out
    assert "1.9.0" in out
    assert "active" in out
    assert "192.168.1.50" in out


def test_more_than_one_container_needs_a_choice_without_a_terminal(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    pve.add_ct(208, tags="pve-flr-portal")
    result = stub(pve, "status")
    assert result.returncode != 0
    assert "207" in result.stderr and "208" in result.stderr
    result = stub(pve, "status", "208")
    assert result.returncode == 0 and "208" in result.stdout


def test_no_managed_containers_is_a_clear_error(pve):
    pve.add_ct(209, tags="web", version=None)
    result = stub(pve, "status")
    assert result.returncode != 0
    assert "adopt" in result.stderr


def test_adopt_tags_existing_installs_and_keeps_other_tags(pve):
    pve.add_ct(207, name="old-install", tags="prod;web")
    pve.add_ct(208, name="not-ours", version=None)
    result = stub(pve, "adopt")
    assert result.returncode == 0
    assert "tags: prod;web;pve-flr-portal" in pve.config(207)
    assert "pve-flr-portal" not in pve.config(208)
    assert "207" in stub(pve, "list").stdout


def test_adopt_a_specific_container_and_refuse_one_without_the_portal(pve):
    pve.add_ct(207)
    pve.add_ct(208, version=None)
    assert stub(pve, "adopt", "207").returncode == 0
    assert "pve-flr-portal" in pve.config(207)
    result = stub(pve, "adopt", "208")
    assert result.returncode != 0
    assert "pve-flr-portal" not in pve.config(208)


def test_config_hands_off_to_the_tools_in_the_containers_checkout(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    pve.install_tools(207)
    result = stub(pve, "config")
    # it ran the management script pulled from the container; that refuses to
    # go on as a non-root user / without a terminal
    assert result.returncode != 0
    assert "root" in result.stderr or "terminal" in result.stderr
    assert "pct pull 207 /opt/pve-flr-portal/deploy/host/pfr-manage.sh" in pve.log("calls.log")
    assert "pct pull 207 /opt/pve-flr-portal/deploy/host/lib/settings.sh" in pve.log("calls.log")


def test_config_on_a_release_without_the_tools_says_to_update_first(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    result = stub(pve, "config")
    assert result.returncode != 0
    assert "runs v1.9.0, a release that predates the management tools" in result.stderr
    assert "pve-flr-portal update 207" in result.stderr


def test_update_passes_its_arguments_to_update_sh_in_the_container(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    assert stub(pve, "update", "207", "v1.4.0", "-y", "--no-snapshot").returncode == 0
    assert "bash /opt/pve-flr-portal/deploy/container/update.sh v1.4.0" in pve.log("exec.log")


def test_the_config_and_update_names_dispatch_to_their_commands(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    assert stub(pve, "-y", "--no-snapshot", name="pve-flr-portal-update").returncode == 0
    assert "deploy/container/update.sh" in pve.log("exec.log")
    result = stub(pve, name="pve-flr-portal-config")
    assert "predates the management tools" in result.stderr


def test_help_and_unknown_commands(pve):
    assert "Usage: pve-flr-portal" in stub(pve, "--help").stdout
    result = stub(pve, "frobnicate")
    assert result.returncode == 2


def test_deploy_scripts_are_executable_in_git():
    # a script that loses its executable bit in git fails on a fresh checkout
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "--stage", "deploy"],
        cwd=DEPLOY.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    scripts = [
        line for line in out.splitlines() if line.split("\t")[1].endswith(".sh") or line.endswith("/pve-flr-portal")
    ]
    scripts = [line for line in scripts if "/lib/" not in line]
    assert len(scripts) >= 10, out
    assert all(line.startswith("100755") for line in scripts), [
        line for line in scripts if not line.startswith("100755")
    ]


def test_an_empty_tools_file_in_the_container_is_never_run(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    deploy = pve.ct(207) / "fs" / "opt" / "pve-flr-portal" / "deploy"
    (deploy / "lib").mkdir(parents=True)
    (deploy / "pfr-manage.sh").write_bytes(b"")
    (deploy / "lib" / "settings.sh").write_bytes(b"")
    result = stub(pve, "config")
    assert result.returncode != 0
    assert "predates the management tools" in result.stderr


def test_progress_is_reported_before_the_slow_steps(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    assert "please wait" in stub(pve, "list").stderr
    result = stub(pve, "config")  # an old release: it still says what it was doing
    assert "Looking for pve-flr-portal containers" in result.stderr
    assert "Fetching the management tools from container 207" in result.stderr


def test_the_host_command_leaves_no_temp_files_behind(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    stub(pve, "config")  # fetches the tools directory, then fails on an old release
    stub(pve, "list")
    assert list(pve.tmpdir.iterdir()) == []
    pve.install_tools(207)
    stub(pve, "config")  # runs the management tool, which refuses without a terminal
    assert list(pve.tmpdir.iterdir()) == []


# --- update ------------------------------------------------------------------


def _updatable(pve: FakePve, ctid: int = 207, version: str = "1.9.0", tags: str = "v1.8.0\nv1.9.0\nv1.10.0\n", **kw):
    pve.add_ct(ctid, tags="pve-flr-portal", version=version, **kw)
    (pve.ct(ctid) / "remote_tags").write_text(tags, encoding="utf-8", newline="\n")


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _new_release_files(pve: FakePve, ctid: int = 207) -> Path:
    return pve.ct(ctid) / "after_update" / "opt" / "pve-flr-portal" / "deploy"


def test_update_says_so_when_already_at_the_latest_release(pve):
    _updatable(pve, version="1.10.0")
    result = stub(pve, "update", "-y")
    assert result.returncode == 0
    assert "up to date (v1.10.0)" in result.stdout
    assert "update.sh" not in pve.log("exec.log")  # nothing was run


def test_update_shows_old_and_new_version_and_runs_update_sh(pve):
    _updatable(pve)
    result = stub(pve, "update", "-y")
    assert result.returncode == 0, result.stderr
    assert "v1.9.0 -> v1.10.0" in result.stdout
    assert "https://github.com/octo/portal/releases/tag/v1.10.0" in result.stdout
    assert "bash /opt/pve-flr-portal/deploy/container/update.sh" in pve.log("exec.log")
    assert "now at v1.10.0" in result.stdout
    assert pve.read(207, "/opt/pve-flr-portal/VERSION").strip() == "1.10.0"


def test_update_refuses_to_guess_without_a_terminal(pve):
    _updatable(pve)
    result = stub(pve, "update")
    assert result.returncode != 0 and "-y" in result.stderr
    assert "update.sh" not in pve.log("exec.log")


def test_update_refreshes_the_host_command_and_the_patch_script_from_the_new_checkout(pve):
    _updatable(pve)
    sbin = pve.root / "sbin"
    _write(sbin / "pve-flr-portal", "#!/usr/bin/env bash\necho old\n")
    _write(sbin / "pve-flr-portal-patch-host-backups.sh", "#!/usr/bin/env bash\necho old\n")
    new = _new_release_files(pve)
    _write(new / "host" / "pve-flr-portal", "#!/usr/bin/env bash\necho new-host-command\n")
    _write(new / "patch-pve-host-backups.sh", "#!/usr/bin/env bash\necho new-patch\n")
    result = stub(pve, "update", "-y")
    assert result.returncode == 0, result.stderr
    assert "new-host-command" in (sbin / "pve-flr-portal").read_text(encoding="utf-8")
    assert "new-patch" in (sbin / "pve-flr-portal-patch-host-backups.sh").read_text(encoding="utf-8")
    assert (sbin / "pve-flr-portal-config").exists()
    assert (sbin / "pve-flr-portal-update").exists()


def test_the_patch_script_is_left_alone_if_it_was_never_installed(pve):
    _updatable(pve)
    _write(_new_release_files(pve) / "patch-pve-host-backups.sh", "#!/usr/bin/env bash\n")
    stub(pve, "update", "-y")
    assert not (pve.root / "sbin" / "pve-flr-portal-patch-host-backups.sh").exists()


def test_a_replacement_that_does_not_parse_is_not_installed(pve):
    _updatable(pve)
    sbin = pve.root / "sbin"
    _write(sbin / "pve-flr-portal", "#!/usr/bin/env bash\necho keep-me\n")
    _write(_new_release_files(pve) / "host" / "pve-flr-portal", "if then fi (((\n")
    result = stub(pve, "update", "-y")
    assert "keep-me" in (sbin / "pve-flr-portal").read_text(encoding="utf-8")
    assert "isn't the expected script" in result.stderr


def test_pinning_an_older_version_leaves_the_host_tools_alone(pve):
    _updatable(pve)
    sbin = pve.root / "sbin"
    _write(sbin / "pve-flr-portal", "#!/usr/bin/env bash\necho keep-me\n")
    _write(_new_release_files(pve) / "host" / "pve-flr-portal", "#!/usr/bin/env bash\necho old-release\n")
    assert stub(pve, "update", "207", "v1.4.0", "-y").returncode == 0
    assert "keep-me" in (sbin / "pve-flr-portal").read_text(encoding="utf-8")


def test_update_can_take_a_snapshot_first(pve):
    _updatable(pve)
    result = stub(pve, "update", "-y", "--snapshot")
    assert result.returncode == 0
    assert "snapshot 207 pfr-pre-update-" in pve.log("set.log")
    assert "pct delsnapshot 207 pfr-pre-update-" in result.stdout


def test_update_goes_on_without_a_snapshot_if_the_storage_cannot_take_one(pve):
    _updatable(pve)
    _write(pve.ct(207) / "no_snapshot", "x\n")
    result = stub(pve, "update", "-y", "--snapshot")
    assert result.returncode == 0  # not a reason to refuse the update
    assert "Could not take a snapshot" in result.stderr
    assert "update.sh" in pve.log("exec.log")


def test_a_failed_update_points_at_the_snapshot_to_roll_back_to(pve):
    _updatable(pve)
    _write(pve.ct(207) / "update_fails", "x\n")
    result = stub(pve, "update", "-y", "--snapshot")
    assert result.returncode != 0
    assert "pct rollback 207 pfr-pre-update-" in result.stderr


def test_update_all_updates_running_containers_and_summarises(pve):
    _updatable(pve, 207, version="1.9.0")
    _updatable(pve, 208, version="1.10.0")
    _updatable(pve, 209, version="1.9.0", status="stopped")
    result = stub(pve, "update", "--all", "-y")
    assert result.returncode == 0, result.stderr
    assert "Skipping container 209" in result.stdout
    assert "Updated: 1, already up to date: 1, skipped: 1, failed: 0." in result.stdout
    assert pve.read(207, "/opt/pve-flr-portal/VERSION").strip() == "1.10.0"
    assert pve.read(209, "/opt/pve-flr-portal/VERSION").strip() == "1.9.0"


def test_update_all_reports_failures_in_its_exit_status(pve):
    _updatable(pve, 207)
    _updatable(pve, 208)
    _write(pve.ct(208) / "update_fails", "x\n")
    result = stub(pve, "update", "--all", "-y")
    assert result.returncode != 0
    assert "Updated: 1, already up to date: 0, skipped: 0, failed: 1." in result.stdout


def test_update_rejects_unknown_options_and_conflicting_arguments(pve):
    _updatable(pve)
    assert stub(pve, "update", "--bogus").returncode != 0
    assert stub(pve, "update", "--all", "207", "-y").returncode != 0


def test_status_says_whether_an_update_is_available(pve):
    _updatable(pve)
    assert "v1.10.0 - update with: pve-flr-portal update 207" in stub(pve, "status").stdout


def test_status_says_when_up_to_date(pve):
    _updatable(pve, version="1.10.0")
    assert "(up to date)" in stub(pve, "status").stdout


def test_update_does_not_replace_the_host_command_with_an_empty_file_when_the_release_lacks_it(pve):
    # `pct pull` leaves an empty file for a missing source - it must not be installed.
    _updatable(pve)
    sbin = pve.root / "sbin"
    _write(sbin / "pve-flr-portal", "#!/usr/bin/env bash\necho keep-me\n")
    result = stub(pve, "update", "-y")
    assert result.returncode == 0, result.stderr
    assert "keep-me" in (sbin / "pve-flr-portal").read_text(encoding="utf-8")
    assert not (sbin / "pve-flr-portal-patch-host-backups.sh").exists()


# --- installs that predate update.sh ---------------------------------------------


def test_a_container_without_update_sh_gets_manual_commands_and_nothing_is_changed(pve):
    _updatable(pve, update_script=False)
    result = stub(pve, "update", "-y", "--snapshot")
    assert result.returncode != 0
    assert "installed before update.sh existed" in result.stderr
    assert "isn't a git checkout" in result.stderr
    assert (
        "pct exec 207 -- bash -c 'git config --system --add safe.directory /opt/pve-flr-portal"
        " && cd /opt/pve-flr-portal && git fetch --tags origin && git checkout --quiet v1.10.0"
        " && INSTALL_CERTBOT=0 bash deploy/install.sh'" in result.stderr
    )
    assert "'pve-flr-portal update' works normally" in result.stderr
    # it stopped before asking, snapshotting or running anything
    assert "snapshot" not in pve.log("set.log")
    assert "update.sh" not in pve.log("exec.log")


def test_the_manual_commands_follow_the_requested_version(pve):
    _updatable(pve, update_script=False)
    assert "git checkout --quiet v1.4.0" in stub(pve, "update", "207", "1.4.0", "-y").stderr
    main = stub(pve, "update", "207", "main", "-y").stderr
    assert "git checkout --quiet main && git merge --quiet --ff-only origin/main" in main


def test_the_manual_commands_work_out_the_latest_tag_when_it_cannot_be_looked_up(pve):
    pve.add_ct(207, tags="pve-flr-portal", version="1.0.0", update_script=False)  # no remote_tags: offline
    err = stub(pve, "update", "-y").stderr
    assert 'git checkout --quiet "$(git tag -l "v*.*.*" --sort=-v:refname | head -1)"' in err


def test_update_all_carries_on_past_a_container_that_needs_a_manual_update(pve):
    _updatable(pve, 207, update_script=False)
    _updatable(pve, 208)
    result = stub(pve, "update", "--all", "-y")
    assert result.returncode != 0
    assert "Updated: 1, already up to date: 0, skipped: 0, failed: 1." in result.stdout
    assert pve.read(208, "/opt/pve-flr-portal/VERSION").strip() == "1.10.0"


# --- legacy installs the command can update for the person ---------------------------


def _legacy(pve: FakePve, ctid: int = 207, version: str = "1.0.0", **kw):
    _updatable(pve, ctid, version=version, update_script=False, git_checkout=True, **kw)


def _exec_script(pve: FakePve) -> str:
    log = pve.log("exec.log")
    return log[log.index("bash -c") :]


def test_a_legacy_container_is_updated_by_running_the_equivalent_steps(pve):
    _legacy(pve)
    _write(_new_release_files(pve) / "container" / "update.sh", "#!/usr/bin/env bash\n")  # the release has update.sh
    result = stub(pve, "update", "-y")
    assert result.returncode == 0, result.stderr
    assert "installed before update.sh existed: the equivalent steps are run" in result.stdout
    script = _exec_script(pve)
    assert "git config --system --add safe.directory /opt/pve-flr-portal" in script
    assert "git fetch --quiet --tags origin" in script
    assert "git checkout --quiet v1.10.0" in script
    assert 'INSTALL_CERTBOT=0 bash "$INSTALL"' in script  # an update doesn't add certbot
    assert "deploy/container/install.sh" in script and "deploy/install.sh" in script  # either layout
    assert "now at v1.10.0" in result.stdout
    # it ends up with update.sh, so the next update takes the normal path
    assert pve.read(207, "/opt/pve-flr-portal/deploy/container/update.sh") is not None


def test_a_legacy_update_is_confirmed_and_can_snapshot_first(pve):
    _legacy(pve)
    assert stub(pve, "update", "207").returncode != 0  # no terminal, no -y: it refuses to guess
    result = stub(pve, "update", "-y", "--snapshot")
    assert result.returncode == 0, result.stderr
    assert "snapshot 207 pfr-pre-update-" in pve.log("set.log")


def test_a_legacy_update_follows_the_requested_version(pve):
    _legacy(pve)
    assert stub(pve, "update", "207", "1.4.0", "-y").returncode == 0
    assert "git checkout --quiet v1.4.0" in _exec_script(pve)


def test_a_legacy_update_with_local_changes_fails_and_names_the_snapshot(pve):
    _legacy(pve)
    _write(pve.ct(207) / "dirty", "x\n")
    result = stub(pve, "update", "-y", "--snapshot")
    assert result.returncode != 0
    assert "local changes" in result.stderr
    assert "The update failed in container 207" in result.stderr
    assert "pct rollback 207 pfr-pre-update-" in result.stderr


def test_a_container_without_git_gets_the_manual_commands_instead(pve):
    _legacy(pve)
    _write(pve.ct(207) / "nogit", "x\n")
    result = stub(pve, "update", "-y")
    assert result.returncode != 0
    assert "isn't a git checkout" in result.stderr
    assert "bash -c" not in pve.log("exec.log")


# --- host-tools ---------------------------------------------------------------------


def test_host_tools_installs_the_command_from_a_containers_checkout(pve):
    _updatable(pve)
    deploy = pve.ct(207) / "fs" / "opt" / "pve-flr-portal" / "deploy"
    _write(deploy / "host" / "pve-flr-portal", "#!/usr/bin/env bash\necho from-the-container\n")
    sbin = pve.root / "sbin"
    result = stub(pve, "host-tools", "207")
    assert result.returncode == 0, result.stderr
    assert "from-the-container" in (sbin / "pve-flr-portal").read_text(encoding="utf-8")
    assert (sbin / "pve-flr-portal-config").exists()
    assert (sbin / "pve-flr-portal-update").exists()
    assert "Updated" in result.stdout
    # again: nothing to do, and it says so
    assert "Already up to date" in stub(pve, "host-tools", "207").stdout


def test_host_tools_also_refreshes_the_patch_script_if_it_was_installed(pve):
    _updatable(pve)
    deploy = pve.ct(207) / "fs" / "opt" / "pve-flr-portal" / "deploy"
    _write(deploy / "host" / "pve-flr-portal", "#!/usr/bin/env bash\necho new\n")
    _write(deploy / "patch-pve-host-backups.sh", "#!/usr/bin/env bash\necho new-patch\n")
    sbin = pve.root / "sbin"
    _write(sbin / "pve-flr-portal-patch-host-backups.sh", "#!/usr/bin/env bash\necho old-patch\n")
    assert stub(pve, "host-tools", "207").returncode == 0
    assert "new-patch" in (sbin / "pve-flr-portal-patch-host-backups.sh").read_text(encoding="utf-8")


def test_host_tools_on_a_release_without_the_command_says_to_update_first(pve):
    _updatable(pve)
    result = stub(pve, "host-tools", "207")
    assert result.returncode != 0
    assert "doesn't include the host command" in result.stderr
    assert "pve-flr-portal update 207" in result.stderr
    assert not (pve.root / "sbin" / "pve-flr-portal").exists()


# --- host-tools from GitHub (a node with no portal container) -------------------------


def _raw(pve: FakePve, ref: str, **files: str) -> Path:
    """FAKE_RAW_DIR/<ref>/<file>: what raw.githubusercontent.com serves for that ref."""
    raw = pve.root / "raw"
    for name, content in files.items():
        _write(raw / ref / name, content)
    return raw


def _github_env(pve: FakePve, raw: Path, latest: str | None = "v1.9.0") -> dict[str, str]:
    env = {"FAKE_RAW_DIR": raw.as_posix(), "REPO_URL": "https://github.com/octo/portal.git"}
    if latest:
        env["FAKE_LATEST_URL"] = f"https://github.com/octo/portal/releases/tag/{latest}"
    return env


NEW_CMD = '#!/usr/bin/env bash\nTAG="pve-flr-portal"\necho from-github\n'


def test_host_tools_without_a_container_downloads_the_latest_release(pve):
    # no containers at all on this node
    raw = _raw(pve, "v1.9.0", **{"pve-flr-portal": NEW_CMD})
    result = stub(pve, "host-tools", env=_github_env(pve, raw))
    assert result.returncode == 0, result.stderr
    assert "Downloaded from v1.9.0" in result.stdout
    sbin = pve.root / "sbin"
    assert "from-github" in (sbin / "pve-flr-portal").read_text(encoding="utf-8")
    assert (sbin / "pve-flr-portal-config").exists()
    assert (sbin / "pve-flr-portal-update").exists()
    assert "Already up to date" in stub(pve, "host-tools", env=_github_env(pve, raw)).stdout


def test_host_tools_falls_back_to_main_when_the_release_predates_the_command(pve):
    raw = _raw(pve, "main", **{"pve-flr-portal": NEW_CMD})  # nothing under v1.9.0
    result = stub(pve, "host-tools", env=_github_env(pve, raw))
    assert result.returncode == 0, result.stderr
    assert "Downloaded from main" in result.stdout


def test_host_tools_honours_pfr_version(pve):
    raw = _raw(pve, "my-branch", **{"pve-flr-portal": NEW_CMD})
    result = stub(pve, "host-tools", env={**_github_env(pve, raw), "PFR_VERSION": "my-branch"})
    assert result.returncode == 0, result.stderr
    assert "Downloaded from my-branch" in result.stdout


def test_host_tools_from_github_refreshes_the_patch_script_only_if_it_was_installed(pve):
    raw = _raw(
        pve,
        "v1.9.0",
        **{"pve-flr-portal": NEW_CMD, "patch-pve-host-backups.sh": "#!/usr/bin/env bash\necho new-patch\n"},
    )
    sbin = pve.root / "sbin"
    stub(pve, "host-tools", env=_github_env(pve, raw))
    assert not (sbin / "pve-flr-portal-patch-host-backups.sh").exists()  # never installed: left alone
    _write(sbin / "pve-flr-portal-patch-host-backups.sh", "#!/usr/bin/env bash\necho old-patch\n")
    stub(pve, "host-tools", env=_github_env(pve, raw))
    assert "new-patch" in (sbin / "pve-flr-portal-patch-host-backups.sh").read_text(encoding="utf-8")


def test_host_tools_never_installs_something_that_is_not_the_command(pve):
    raw = _raw(pve, "v1.9.0", **{"pve-flr-portal": "#!/usr/bin/env bash\necho not-the-command\n"})
    sbin = pve.root / "sbin"
    _write(sbin / "pve-flr-portal", "#!/usr/bin/env bash\necho keep-me\n")
    result = stub(pve, "host-tools", env=_github_env(pve, raw))
    assert "keep-me" in (sbin / "pve-flr-portal").read_text(encoding="utf-8")
    assert "isn't the expected script" in result.stderr


def test_host_tools_says_what_to_do_when_github_cannot_be_reached(pve):
    raw = pve.root / "raw"  # serves nothing
    result = stub(pve, "host-tools", env=_github_env(pve, raw))
    assert result.returncode != 0
    assert "Could not download the host command from GitHub" in result.stderr
    assert "give a container ID" in result.stderr


def test_host_tools_rejects_something_that_is_not_a_container_id(pve):
    result = stub(pve, "host-tools", "not-an-id")
    assert result.returncode != 0
    assert "not a container ID" in result.stderr


# --- install: create a new container from the installer on GitHub ----------------------

FAKE_INSTALLER = (
    '#!/usr/bin/env bash\nPFR_LIB_API_WANTED=1\necho "INSTALLER-RAN args=[$*] version=[${PFR_VERSION:-}]"\n'
)


def test_install_downloads_the_installer_from_the_latest_release_and_runs_it(pve):
    raw = _raw(pve, "v1.9.0", **{"lxc-create.sh": FAKE_INSTALLER})
    result = stub(pve, "install", env=_github_env(pve, raw))
    assert result.returncode == 0, result.stderr
    assert "INSTALLER-RAN" in result.stdout
    assert "Using the installer from v1.9.0" in result.stderr
    assert "Fetching the installer from GitHub - please wait" in result.stderr


def test_install_falls_back_to_main_and_honours_pfr_version(pve):
    raw = _raw(pve, "main", **{"lxc-create.sh": FAKE_INSTALLER})
    result = stub(pve, "install", env=_github_env(pve, raw))
    assert result.returncode == 0, result.stderr
    assert "Using the installer from main" in result.stderr

    raw = _raw(pve, "my-branch", **{"lxc-create.sh": FAKE_INSTALLER})
    result = stub(pve, "install", env={**_github_env(pve, raw), "PFR_VERSION": "my-branch"})
    assert "Using the installer from my-branch" in result.stderr
    assert "version=[my-branch]" in result.stdout  # the installer sees it too, so it installs the same version


def test_install_passes_its_arguments_to_the_installer(pve):
    raw = _raw(pve, "v1.9.0", **{"lxc-create.sh": FAKE_INSTALLER})
    result = stub(pve, "install", "--some-option", env=_github_env(pve, raw))
    assert "args=[--some-option]" in result.stdout


def test_install_never_runs_something_that_is_not_the_installer(pve):
    for content in ("", "<html>404: Not Found</html>\n", "#!/usr/bin/env bash\necho not-the-installer\n"):
        raw = _raw(pve, "v1.9.0", **{"lxc-create.sh": content})
        result = stub(pve, "install", env=_github_env(pve, raw))
        assert result.returncode != 0
        assert "not-the-installer" not in result.stdout
        assert "Could not download the installer" in result.stderr


def test_install_says_so_when_github_cannot_be_reached(pve):
    result = stub(pve, "install", env=_github_env(pve, pve.root / "raw"))
    assert result.returncode != 0
    assert "Could not download the installer from GitHub" in result.stderr


def test_install_leaves_no_temp_files_behind(pve):
    raw = _raw(pve, "v1.9.0", **{"lxc-create.sh": FAKE_INSTALLER})
    stub(pve, "install", env=_github_env(pve, raw))
    stub(pve, "install", env=_github_env(pve, pve.root / "nothing"))
    assert list(pve.tmpdir.iterdir()) == []


# --- roles: the roles tool, from GitHub --------------------------------------------------


def _roles_tool(pve: FakePve, ref: str = "v1.9.0", content: str | None = None) -> Path:
    from pve_fakes import DEPLOY as D

    text = content if content is not None else (D / "host" / "pfr-roles.sh").read_text(encoding="utf-8")
    return _raw(pve, ref, **{"pfr-roles.sh": text})


def test_roles_runs_the_tool_downloaded_from_the_latest_release(pve):
    raw = _roles_tool(pve)
    result = stub(pve, "roles", "status", env=_github_env(pve, raw))
    assert result.returncode == 0, result.stderr
    assert "FileRestoreReader" in result.stdout
    assert "Using the roles tool from v1.9.0" in result.stderr
    assert "roles tool isn't installed on this host yet" in result.stderr  # no local copy: downloaded for this run


def test_roles_passes_its_arguments_through_and_changes_pve(pve):
    raw = _roles_tool(pve)
    result = stub(pve, "roles", "create", "FileRestoreReader", env=_github_env(pve, raw))
    assert result.returncode == 0, result.stderr
    assert "Created FileRestoreReader" in result.stdout
    assert "FileRestoreReader" in pve.roles()


def test_roles_falls_back_to_main_and_honours_pfr_version(pve):
    raw = _roles_tool(pve, ref="main")
    assert "from main" in stub(pve, "roles", "status", env=_github_env(pve, raw)).stderr
    raw = _roles_tool(pve, ref="my-branch")
    result = stub(pve, "roles", "status", env={**_github_env(pve, raw), "PFR_VERSION": "my-branch"})
    assert "from my-branch" in result.stderr


def test_roles_never_runs_something_that_is_not_the_roles_tool(pve):
    for content in ("", "<html>404</html>\n", "#!/usr/bin/env bash\necho not-the-tool\n"):
        raw = _roles_tool(pve, content=content)
        result = stub(pve, "roles", "status", env=_github_env(pve, raw))
        assert result.returncode != 0
        assert "not-the-tool" not in result.stdout
        assert "Could not download the roles tool" in result.stderr


def test_roles_says_so_when_github_cannot_be_reached(pve):
    result = stub(pve, "roles", "status", env=_github_env(pve, pve.root / "raw"))
    assert result.returncode != 0
    assert "Could not download the roles tool from GitHub" in result.stderr


def test_roles_leaves_no_temp_files_behind(pve):
    raw = _roles_tool(pve)
    stub(pve, "roles", "status", env=_github_env(pve, raw))
    assert list(pve.tmpdir.iterdir()) == []


# --- the roles tool lives on the host ----------------------------------------------------


def _local_roles_tool(pve: FakePve, body: str | None = None) -> Path:
    from pve_fakes import DEPLOY as D

    text = body if body is not None else (D / "host" / "pfr-roles.sh").read_text(encoding="utf-8")
    path = pve.root / "sbin" / "pve-flr-portal-roles"
    _write(path, text)
    return path


def test_roles_uses_the_copy_on_the_host_without_touching_the_network(pve):
    _local_roles_tool(pve)
    # nothing is served and no release exists: if it tried to download, it would fail
    result = stub(pve, "roles", "status", env=_github_env(pve, pve.root / "nothing"))
    assert result.returncode == 0, result.stderr
    assert "FileRestoreReader" in result.stdout
    assert "Fetching" not in result.stderr and "GitHub" not in result.stderr


def test_host_tools_installs_the_roles_tool_from_github(pve):
    from pve_fakes import DEPLOY as D

    raw = _raw(
        pve,
        "v1.9.0",
        **{"pve-flr-portal": NEW_CMD, "pfr-roles.sh": (D / "host" / "pfr-roles.sh").read_text(encoding="utf-8")},
    )
    result = stub(pve, "host-tools", env=_github_env(pve, raw))
    assert result.returncode == 0, result.stderr
    tool = pve.root / "sbin" / "pve-flr-portal-roles"
    assert "PFR_ROLES_SCRIPT=1" in tool.read_text(encoding="utf-8")
    assert "Already up to date" in stub(pve, "host-tools", env=_github_env(pve, raw)).stdout


def test_host_tools_from_github_never_installs_a_roles_tool_that_is_not_the_tool(pve):
    raw = _raw(pve, "v1.9.0", **{"pve-flr-portal": NEW_CMD, "pfr-roles.sh": "#!/usr/bin/env bash\necho not-it\n"})
    result = stub(pve, "host-tools", env=_github_env(pve, raw))
    assert result.returncode == 0, result.stderr
    assert not (pve.root / "sbin" / "pve-flr-portal-roles").exists()
    assert "isn't the expected script" in result.stderr


def test_host_tools_from_a_container_installs_the_roles_tool_too(pve):
    from pve_fakes import DEPLOY as D

    _updatable(pve)
    deploy = pve.ct(207) / "fs" / "opt" / "pve-flr-portal" / "deploy"
    _write(deploy / "host" / "pve-flr-portal", "#!/usr/bin/env bash\necho cmd\n")
    _write(deploy / "host" / "pfr-roles.sh", (D / "host" / "pfr-roles.sh").read_text(encoding="utf-8"))
    assert stub(pve, "host-tools", "207").returncode == 0
    assert (pve.root / "sbin" / "pve-flr-portal-roles").exists()


def test_a_container_update_refreshes_the_roles_tool_too(pve):
    from pve_fakes import DEPLOY as D

    _updatable(pve)
    _local_roles_tool(pve, "#!/usr/bin/env bash\nPFR_ROLES_SCRIPT=1\necho old\n")
    new = _new_release_files(pve)
    _write(new / "host" / "pve-flr-portal", "#!/usr/bin/env bash\necho cmd\n")
    _write(new / "host" / "pfr-roles.sh", (D / "host" / "pfr-roles.sh").read_text(encoding="utf-8"))
    assert stub(pve, "update", "-y").returncode == 0
    assert "Usage: pfr-roles.sh" in (pve.root / "sbin" / "pve-flr-portal-roles").read_text(encoding="utf-8")
