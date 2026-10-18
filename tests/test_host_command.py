"""deploy/host/pve-flr-portal - the host command: finding the containers it
manages by tag, choosing one, adopting existing installs, and handing off to
the management tools in the container's own checkout. Run against a fake
`pct` (tests/fakes/pct)."""

import shutil

import pytest
from pve_fakes import DEPLOY, FakePve

STUB = DEPLOY / "host" / "pve-flr-portal"


@pytest.fixture
def pve():
    fake = FakePve()
    yield fake
    fake.close()


def stub(pve: FakePve, *args: str, stdin: str | None = None, name: str = "pve-flr-portal"):
    script = pve.root / name
    if not script.exists():
        shutil.copy(STUB, script)
    return pve.run_script(script, list(args), stdin=stdin)


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
    assert "pct pull 207 /opt/pve-flr-portal/deploy/pfr-manage.sh" in pve.log("calls.log")
    assert "pct pull 207 /opt/pve-flr-portal/deploy/lib/settings.sh" in pve.log("calls.log")


def test_config_on_a_release_without_the_tools_says_to_update_first(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    result = stub(pve, "config")
    assert result.returncode != 0
    assert "runs v1.9.0, a release that predates the management tools" in result.stderr
    assert "pve-flr-portal update 207" in result.stderr


def test_update_passes_its_arguments_to_update_sh_in_the_container(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    assert stub(pve, "update", "207", "v1.4.0").returncode == 0
    assert "bash /opt/pve-flr-portal/deploy/update.sh v1.4.0" in pve.log("exec.log")


def test_the_config_and_update_names_dispatch_to_their_commands(pve):
    pve.add_ct(207, tags="pve-flr-portal")
    assert stub(pve, name="pve-flr-portal-update").returncode == 0
    assert "deploy/update.sh" in pve.log("exec.log")
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
        ["git", "ls-files", "--stage", "deploy/pfr-manage.sh", "deploy/host/pve-flr-portal", "deploy/lxc-create.sh"],
        cwd=DEPLOY.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.count("100755") == 3, out


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
