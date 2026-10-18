"""deploy/pfr-roles.sh - creating, inspecting and deleting the Proxmox roles the
portal's users need, against a fake pveum/pveversion (tests/fakes/)."""

import pytest
from pve_fakes import DEPLOY, FakePve

SCRIPT = DEPLOY / "pfr-roles.sh"

READER = "Datastore.AllocateSpace,VM.Backup,VM.Audit"
OPERATOR = "VM.GuestAgent.Audit,VM.GuestAgent.FileWrite"
HOST = "Datastore.Allocate,Datastore.Audit"


@pytest.fixture
def pve():
    fake = FakePve()
    yield fake
    fake.close()


def roles(pve: FakePve, *args: str, major: int = 9, stdin: str | None = None):
    return pve.run_script(SCRIPT, list(args), {"FAKE_PVE_MAJOR": str(major)}, stdin)


def sorted_privs(privs: str) -> str:
    return ",".join(sorted(privs.split(",")))


# --- status ----------------------------------------------------------------------


def test_status_of_a_fresh_install_shows_everything_missing(pve):
    out = roles(pve, "status").stdout
    for name in ("FileRestoreReader", "FileRestoreOperator", "FileRestoreReaderHost"):
        assert name in out
    assert out.count("missing") == 3


def test_status_distinguishes_ok_differing_and_used_roles(pve):
    pve.add_role("FileRestoreReader", "VM.Audit,Datastore.AllocateSpace,VM.Backup")  # order doesn't matter
    pve.add_role("FileRestoreReaderHost", "Datastore.Audit")  # not what is documented
    pve.add_acl("/vms", "alice@pam", "FileRestoreReader")
    pve.add_acl("/storage/pbs", "alice@pam", "FileRestoreReader")
    out = roles(pve, "status").stdout
    reader = next(line for line in out.splitlines() if line.startswith("FileRestoreReader "))
    assert "ok" in reader and " 2 " in reader  # two grants
    host = next(line for line in out.splitlines() if line.startswith("FileRestoreReaderHost"))
    assert "differs" in host


def test_status_on_pve_8_says_the_operator_role_needs_pve_9(pve):
    out = roles(pve, "status", major=8).stdout
    operator = next(line for line in out.splitlines() if line.startswith("FileRestoreOperator"))
    assert "n/a" in operator and "PVE 9" in operator


# --- create ------------------------------------------------------------------------


def test_create_adds_the_documented_privileges(pve):
    result = roles(pve, "create", "FileRestoreReader")
    assert result.returncode == 0, result.stderr
    assert "Created FileRestoreReader" in result.stdout
    assert f"pveum role add FileRestoreReader -privs {READER}" in pve.log("pveum.log")
    assert pve.roles() == {"FileRestoreReader": READER}
    assert "grants nothing" in result.stdout  # a reminder that users still need an ACL


def test_create_is_idempotent(pve):
    roles(pve, "create", "FileRestoreReader")
    result = roles(pve, "create", "FileRestoreReader")
    assert result.returncode == 0
    assert "already exists" in result.stdout
    assert pve.log("pveum.log").count("role add FileRestoreReader") == 1


def test_create_all_makes_the_three_roles_and_warns_about_the_host_role(pve):
    result = roles(pve, "create", "--all")
    assert result.returncode == 0, result.stderr
    assert set(pve.roles()) == {"FileRestoreReader", "FileRestoreOperator", "FileRestoreReaderHost"}
    assert pve.roles()["FileRestoreReaderHost"] == HOST
    assert "full admin rights on a storage" in result.stdout


def test_the_operator_role_is_skipped_on_pve_8(pve):
    result = roles(pve, "create", "--all", major=8)
    assert result.returncode == 0, result.stderr
    assert "Skipping FileRestoreOperator: it needs PVE 9 or newer" in result.stdout
    assert set(pve.roles()) == {"FileRestoreReader", "FileRestoreReaderHost"}


def test_unrestricted_is_only_added_when_asked_for(pve):
    roles(pve, "create", "FileRestoreOperator")
    assert pve.roles()["FileRestoreOperator"] == OPERATOR
    pve2 = FakePve()
    try:
        roles(pve2, "create", "FileRestoreOperator", "--unrestricted")
        assert sorted_privs(pve2.roles()["FileRestoreOperator"]) == sorted_privs(
            OPERATOR + ",VM.GuestAgent.Unrestricted"
        )
    finally:
        pve2.close()


def test_an_operator_role_that_already_has_unrestricted_is_left_alone_without_the_flag(pve):
    pve.add_role("FileRestoreOperator", OPERATOR + ",VM.GuestAgent.Unrestricted")
    result = roles(pve, "create", "FileRestoreOperator")
    assert "already exists" in result.stdout
    assert "role modify" not in pve.log("pveum.log")


def test_a_role_with_different_privileges_is_not_changed_without_confirmation(pve):
    pve.add_role("FileRestoreReader", "VM.Audit")
    result = roles(pve, "create", "FileRestoreReader")  # no terminal, no -y
    assert result.returncode == 1
    assert "exists with different privileges" in result.stdout
    assert "has:    VM.Audit" in result.stdout
    assert "Left FileRestoreReader as it is" in result.stderr
    assert pve.roles()["FileRestoreReader"] == "VM.Audit"


def test_yes_changes_a_differing_role(pve):
    pve.add_role("FileRestoreReader", "VM.Audit")
    result = roles(pve, "create", "FileRestoreReader", "-y")
    assert result.returncode == 0, result.stderr
    assert "Changed FileRestoreReader" in result.stdout
    assert pve.roles()["FileRestoreReader"] == READER


def test_create_needs_names_and_rejects_unknown_ones(pve):
    result = roles(pve, "create")
    assert result.returncode != 0 and "Say which roles" in result.stderr
    result = roles(pve, "create", "NoSuchRole")
    assert result.returncode != 0 and "Unknown role 'NoSuchRole'" in result.stderr
    result = roles(pve, "create", "FileRestoreReader", "--bogus")
    assert result.returncode != 0 and "Unknown option" in result.stderr
    assert pve.roles() == {}


# --- delete ------------------------------------------------------------------------


def test_delete_removes_an_unused_role_after_confirmation(pve):
    pve.add_role("FileRestoreReader", READER)
    assert roles(pve, "delete", "FileRestoreReader").returncode == 1  # no terminal, no -y: it won't guess
    assert "FileRestoreReader" in pve.roles()
    result = roles(pve, "delete", "FileRestoreReader", "-y")
    assert result.returncode == 0, result.stderr
    assert "Deleted FileRestoreReader" in result.stdout
    assert pve.roles() == {}


def test_delete_refuses_while_grants_use_the_role(pve):
    pve.add_role("FileRestoreReader", READER)
    pve.add_acl("/vms", "alice@pam", "FileRestoreReader")
    result = roles(pve, "delete", "FileRestoreReader", "-y")
    assert result.returncode == 1
    assert "is used by these grants" in result.stdout
    assert "/vms  user alice@pam" in result.stdout
    assert "pass --remove-acls" in result.stderr
    assert "FileRestoreReader" in pve.roles()
    assert "acl delete" not in pve.log("pveum.log")


def test_remove_acls_deletes_the_grants_then_the_role(pve):
    pve.add_role("FileRestoreReader", READER)
    pve.add_role("FileRestoreOperator", OPERATOR)
    pve.add_acl("/vms", "alice@pam", "FileRestoreReader")
    pve.add_acl("/storage/pbs", "admins", "FileRestoreReader", kind="group")
    pve.add_acl("/vms", "bob@pam", "FileRestoreOperator")  # a different role: untouched
    result = roles(pve, "delete", "FileRestoreReader", "--remove-acls", "-y")
    assert result.returncode == 0, result.stderr
    log = pve.log("pveum.log")
    assert "acl delete /vms --roles FileRestoreReader --users alice@pam" in log
    assert "acl delete /storage/pbs --roles FileRestoreReader --groups admins" in log
    assert pve.roles() == {"FileRestoreOperator": OPERATOR}
    assert pve.acls() == ["/vms\tuser\tbob@pam\tFileRestoreOperator"]


def test_remove_acls_still_asks_without_yes(pve):
    pve.add_role("FileRestoreReader", READER)
    pve.add_acl("/vms", "alice@pam", "FileRestoreReader")
    result = roles(pve, "delete", "FileRestoreReader", "--remove-acls")  # no terminal
    assert result.returncode == 1
    assert "FileRestoreReader" in pve.roles()
    assert len(pve.acls()) == 1


def test_deleting_a_role_that_does_not_exist_is_fine(pve):
    result = roles(pve, "delete", "FileRestoreReader", "-y")
    assert result.returncode == 0
    assert "doesn't exist" in result.stdout


def test_help_and_the_script_mode(pve):
    assert "Usage: pfr-roles.sh" in roles(pve, "--help").stdout
    assert roles(pve, "frobnicate").returncode == 2


def test_the_script_is_executable_in_git():
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "--stage", "deploy/pfr-roles.sh"],
        cwd=DEPLOY.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.startswith("100755"), out
