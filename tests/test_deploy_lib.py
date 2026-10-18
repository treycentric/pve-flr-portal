"""deploy/lib/settings.sh - the pieces shared by the installer and the
management tool: the container's net0 value (built and parsed), the portal's
.env (read, and patched in place), Direct Network Transfer's mapping, and the
settings registry. Run in bash against a fake `pct` (tests/fakes/pct)."""

import subprocess

import pytest
from pve_fakes import DEPLOY, FakePve

LIB = (DEPLOY / "lib" / "settings.sh").as_posix()


@pytest.fixture
def pve():
    fake = FakePve()
    yield fake
    fake.close()


def run(pve: FakePve, body: str):
    result = pve.bash(f'set -euo pipefail\nsource "{LIB}"\n{body}\n')
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# --- net0 ----------------------------------------------------------------


def test_build_net0_for_a_new_container(pve):
    out = run(pve, 'BRIDGE=vmbr0 USE_STATIC_IP=no IPV6_MODE=auto VLAN_TAG=""; IP_CONFIG=""; pfr_build_net0')
    assert out == "name=eth0,bridge=vmbr0,ip=dhcp,ip6=auto"


def test_build_net0_static_both_families_and_vlan(pve):
    out = run(
        pve,
        'BRIDGE=vmbr20 VLAN_TAG=100 USE_STATIC_IP=yes STATIC_CIDR=10.0.0.5/24 STATIC_GW=10.0.0.1 IP_CONFIG=""\n'
        "IPV6_MODE=static STATIC6_CIDR=2001:db8::5/64 STATIC6_GW=2001:db8::1\n"
        "pfr_build_net0",
    )
    assert out == "name=eth0,bridge=vmbr20,tag=100,ip=10.0.0.5/24,gw=10.0.0.1,ip6=2001:db8::5/64,gw6=2001:db8::1"


@pytest.mark.parametrize("mode", ["disabled", "none"])
def test_build_net0_leaves_out_ip6_when_disabled_or_unmanaged(pve, mode):
    out = run(pve, f'BRIDGE=vmbr0 VLAN_TAG="" USE_STATIC_IP=no IPV6_MODE={mode} IP_CONFIG=""; pfr_build_net0')
    assert "ip6" not in out


def test_build_net0_keeps_fields_it_does_not_manage(pve):
    original = "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:CC,firewall=1,mtu=1400,ip=dhcp,type=veth,tag=5,ip6=auto"
    out = run(
        pve,
        f'BRIDGE=vmbr2 VLAN_TAG="" USE_STATIC_IP=no IPV6_MODE=dhcp IP_CONFIG=""; pfr_build_net0 "{original}"',
    )
    fields = dict(part.split("=", 1) for part in out.split(","))
    # the MAC (a DHCP reservation may hang off it) and hand-set options survive
    assert fields["hwaddr"] == "BC:24:11:AA:BB:CC"
    assert fields["firewall"] == "1"
    assert fields["mtu"] == "1400"
    assert fields["type"] == "veth"
    # what the tool manages is rebuilt
    assert fields["bridge"] == "vmbr2"
    assert "tag" not in fields
    assert fields["ip"] == "dhcp"
    assert fields["ip6"] == "dhcp"


def test_parse_net0_reads_the_managed_fields(pve):
    out = run(
        pve,
        'pfr_parse_net0 "name=eth0,bridge=vmbr3,tag=30,hwaddr=AA,ip=10.1.1.5/24,gw=10.1.1.1,'
        'ip6=2001:db8::5/64,gw6=2001:db8::1"\n'
        'echo "$BRIDGE|$VLAN_TAG|$USE_STATIC_IP|$STATIC_CIDR|$STATIC_GW|$IPV6_MODE|$STATIC6_CIDR|$STATIC6_GW"',
    )
    assert out == "vmbr3|30|yes|10.1.1.5/24|10.1.1.1|static|2001:db8::5/64|2001:db8::1"


@pytest.mark.parametrize(
    ("net0", "expected"),
    [
        ("name=eth0,bridge=vmbr0,ip=dhcp,ip6=auto", "no|auto"),
        ("name=eth0,bridge=vmbr0,ip=dhcp,ip6=dhcp", "no|dhcp"),
        ("name=eth0,bridge=vmbr0,ip=dhcp", "no|none"),
        ("name=eth0,bridge=vmbr0,ip=manual,ip6=manual", "manual|manual"),
    ],
)
def test_parse_net0_special_values(pve, net0, expected):
    out = run(pve, f'pfr_parse_net0 "{net0}"; echo "$USE_STATIC_IP|$IPV6_MODE"')
    assert out == expected


def test_net0_survives_a_build_parse_round_trip(pve):
    out = run(
        pve,
        "BRIDGE=vmbr7 VLAN_TAG=7 USE_STATIC_IP=yes STATIC_CIDR=10.7.0.9/24 STATIC_GW=10.7.0.1"
        ' IP_CONFIG="" IPV6_MODE=auto\n'
        "net=$(pfr_build_net0)\n"
        'BRIDGE=x VLAN_TAG="" USE_STATIC_IP=no STATIC_CIDR="" STATIC_GW="" IPV6_MODE=none\n'
        'pfr_parse_net0 "$net"\n'
        'echo "$BRIDGE|$VLAN_TAG|$USE_STATIC_IP|$STATIC_CIDR|$STATIC_GW|$IPV6_MODE"',
    )
    assert out == "vmbr7|7|yes|10.7.0.9/24|10.7.0.1|auto"


# --- .env ------------------------------------------------------------------


def test_load_env_reads_backed_settings_and_maps_one_data_network(pve, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        '# comment\nPVE_HOST=pve.example\nPVE_STORAGE="pbs-a,pbs-b"\nPORT=9443\nPVE_VERIFY_SSL=/etc/ca.pem\n'
        'RESTORE_DATA_NICS=[{"cidr":"10.0.5.0/24","local_ip":"10.0.5.5","hostname":"flr-data"}]\nUNKNOWN_SETTING=x\n',
        encoding="utf-8",
    )
    out = run(
        pve,
        f'pfr_init_install\npfr_load_env "{env.as_posix()}"\n'
        'echo "$PVE_HOST|$PVE_STORAGE|$PORTAL_PORT|$PVE_VERIFY_SSL|$ENABLE_DNT|$DNT_CIDR|$DNT_LOCAL_IP|$DNT_HOSTNAME"',
    )
    assert out == "pve.example|pbs-a,pbs-b|9443|/etc/ca.pem|yes|10.0.5.0/24|10.0.5.5|flr-data"


@pytest.mark.parametrize(
    ("nics", "expected"),
    [
        ("[]", "no"),
        ("", "no"),
        ('[{"cidr":"a","local_ip":"b"},{"cidr":"c","local_ip":"d"}]', "custom"),
        ("not json", "custom"),
        ('[{"cidr":"a","local_ip":"b","extra":1}]', "custom"),
    ],
)
def test_data_networks_the_tool_cannot_represent_are_not_touched(pve, tmp_path, nics, expected):
    env = tmp_path / ".env"
    env.write_text(f"RESTORE_DATA_NICS={nics}\n", encoding="utf-8")
    out = run(pve, f'pfr_init_install\npfr_load_env "{env.as_posix()}"\necho "$ENABLE_DNT"')
    assert out == expected


def test_config_mode_collects_only_changed_settings(pve):
    out = run(
        pve,
        "pfr_init_install\npfr_use_config_layout\npfr_snapshot\n"
        "DEFAULT_THEME=dark\nPORTAL_PORT=9443\n"
        'pfr_collect_env_overrides; printf "%s\\n" "${ENV_OVERRIDES[@]}"',
    )
    assert sorted(out.splitlines()) == ["DEFAULT_THEME=dark", "PORT=9443"]


def test_write_env_patches_in_place_and_appends_new_keys(pve):
    pve.add_ct(207)
    out = run(
        pve,
        "PFR_ENV_FILE=/opt/pve-flr-portal/.env PFR_OVERRIDES_TMP=/tmp/pfr-overrides.env\n"
        'ENV_OVERRIDES=("DEFAULT_THEME=dark" "JOB_HISTORY_RETENTION_DAYS=30")\n'
        "pfr_write_env 207\ncat /dev/null",
    )
    assert out == ""
    env = pve.read(207, "/opt/pve-flr-portal/.env")
    assert "DEFAULT_THEME=dark\n" in env  # replaced where it was
    assert env.index("DEFAULT_THEME=dark") < env.index("RESTORE_DATA_NICS")
    assert env.rstrip().endswith("JOB_HISTORY_RETENTION_DAYS=30")  # appended
    # everything else is untouched, comments included
    assert env.startswith("# portal settings\nPVE_HOST=titan.example.net\n")
    assert 'RESTORE_DATA_NICS=[{"cidr": "192.168.1.0/24", "local_ip": "192.168.1.50"}]' in env


def test_container_address_and_data_network_resolution(pve):
    pve.add_ct(207, addr="192.168.7.20/24")
    out = run(pve, "pfr_ct_address 207")
    assert out == "192.168.7.0/24 192.168.7.20"
    out = run(
        pve,
        'ENABLE_DNT=yes DNT_CIDR="" DNT_LOCAL_IP="" DNT_HOSTNAME="" RESTORE_DATA_NICS_JSON=""\n'
        'pfr_resolve_dnt 207 >/dev/null; echo "$RESTORE_DATA_NICS_JSON"',
    )
    assert out == '[{"cidr":"192.168.7.0/24","local_ip":"192.168.7.20"}]'


def test_data_network_resolution_gives_up_cleanly_without_an_address(pve):
    pve.add_ct(207, addr=None)
    out = run(
        pve,
        'ENABLE_DNT=yes DNT_CIDR="" DNT_LOCAL_IP="" DNT_HOSTNAME="" RESTORE_DATA_NICS_JSON=""\n'
        'pfr_resolve_dnt 207 2>/dev/null; echo "$ENABLE_DNT|$RESTORE_DATA_NICS_JSON"',
    )
    assert out == "no|"


# --- registry ----------------------------------------------------------------


def test_every_setting_backed_by_env_is_known_to_the_example_file(pve):
    env_example = (DEPLOY.parent / ".env.example").read_text(encoding="utf-8")
    out = run(pve, 'for k in "${ALL_KEYS[@]}"; do if [ -n "${S_ENV[$k]}" ]; then echo "${S_ENV[$k]}"; fi; done')
    for key in out.splitlines():
        assert f"\n{key}=" in env_example or env_example.startswith(f"{key}="), key


def test_config_layout_leaves_out_what_cannot_change_after_install(pve):
    out = run(pve, 'pfr_use_config_layout; echo "${BASIC_LAYOUT[*]}|${ADV_GROUPS[*]}"')
    layout, groups = out.split("|")
    for hidden in ("CTID", "STORAGE", "ENABLE_HOST_BACKUPS"):
        assert hidden not in layout.split()
    assert "resources" not in groups.split()
    assert "cert" not in groups.split()


def _dialog_args(pve: FakePve, body: str) -> str:
    run(pve, f"IS_TTY=1; HAS_WHIPTAIL=1; export FAKE_WT_ANSWER=x\n{body}")
    return pve.log("wt.log")


def test_menus_and_checklists_do_not_start_focus_in_the_text(pve):
    # --scrolltext moves the initial focus onto the text's scroll arrow, so
    # people had to tab to reach the list - it is only used when the text
    # really can't fit.
    log = _dialog_args(
        pve,
        'BRIDGE=vmbr0; ui_pick BRIDGE "Bridge" "Choose a bridge." "vmbr0" "vmbr1"\n'
        'PVE_STORAGE=a; ui_multi PVE_STORAGE "Storage" "Pick some." "a" "b"',
    )
    assert "--menu" in log and "--checklist" in log
    assert "--scrolltext" not in log


def test_very_long_text_scrolls_instead_of_being_cut_off(pve):
    long_text = "word " * 600
    log = _dialog_args(pve, f'BRIDGE=vmbr0; ui_pick BRIDGE "Bridge" "{long_text}" "vmbr0" "vmbr1"')
    assert "--scrolltext" in log


def test_the_installer_welcome_says_installer(pve):
    log = _dialog_args(pve, "show_welcome")
    assert "Welcome to the pve-flr-portal installer" in log
    assert "configurator" not in log
    assert "--backtitle pve-flr-portal setup" in log


def test_the_configurator_welcome_says_configurator(pve):
    log = _dialog_args(pve, "pfr_use_config_layout; show_welcome")
    assert "Welcome to the pve-flr-portal configurator" in log
    assert "installer" not in log
    assert "--backtitle pve-flr-portal configuration" in log


# --- copying files out of a container -----------------------------------------


def _pull(pve: FakePve, src: str, dest) -> subprocess.CompletedProcess:
    return pve.bash(f'set -uo pipefail\nsource "{LIB}"\npfr_pull 207 {src} "{dest.as_posix()}"; echo "rc=$?"\n')


def test_pull_copies_an_existing_file(pve, tmp_path):
    pve.add_ct(207)
    dest = tmp_path / "out.txt"
    result = _pull(pve, "/opt/pve-flr-portal/VERSION", dest)
    assert "rc=0" in result.stdout
    assert dest.read_text(encoding="utf-8").strip() == "1.9.0"


def test_pull_of_a_missing_file_fails_and_leaves_the_destination_alone(pve, tmp_path):
    # `pct pull` reports success and writes an empty file for a missing source;
    # that must never reach the destination.
    pve.add_ct(207)
    dest = tmp_path / "out.txt"
    dest.write_text("keep me\n", encoding="utf-8", newline="\n")
    result = _pull(pve, "/opt/pve-flr-portal/deploy/host/pve-flr-portal", dest)
    assert "rc=1" in result.stdout
    assert dest.read_text(encoding="utf-8") == "keep me\n"


def test_pull_of_an_empty_file_fails(pve, tmp_path):
    pve.add_ct(207)
    (pve.ct(207) / "fs" / "opt" / "pve-flr-portal" / "empty").write_bytes(b"")
    dest = tmp_path / "out.txt"
    result = _pull(pve, "/opt/pve-flr-portal/empty", dest)
    assert "rc=1" in result.stdout
    assert not dest.exists()


def test_the_host_command_is_installed_from_the_containers_checkout(pve, tmp_path):
    pve.add_ct(207)
    deploy = pve.ct(207) / "fs" / "opt" / "pve-flr-portal" / "deploy" / "host"
    deploy.mkdir(parents=True)
    (deploy / "pve-flr-portal").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8", newline="\n")
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    result = pve.bash(
        f'set -uo pipefail\nsource "{LIB}"\nPFR_SBIN="{sbin.as_posix()}" pfr_install_host_command 207; echo "rc=$?"\n'
    )
    assert "rc=0" in result.stdout, result.stderr
    assert "echo hi" in (sbin / "pve-flr-portal").read_text(encoding="utf-8")
    assert (sbin / "pve-flr-portal-config").exists()
    assert (sbin / "pve-flr-portal-update").exists()


def test_a_release_without_the_host_command_installs_nothing(pve, tmp_path):
    # This is what produced a 0-byte /usr/local/sbin/pve-flr-portal.
    pve.add_ct(207)
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    result = pve.bash(
        f'set -uo pipefail\nsource "{LIB}"\nPFR_SBIN="{sbin.as_posix()}" pfr_install_host_command 207; echo "rc=$?"\n'
    )
    assert "rc=1" in result.stdout
    assert list(sbin.iterdir()) == []


# --- the review screen's own proceed button -----------------------------------


def _hub(pve: FakePve, script_lines: list[str], mode_setup: str = "pfr_init_install", extra: str = "") -> tuple:
    script = pve.root / "wt_script.txt"
    script.write_text("\n".join(script_lines) + "\n", encoding="utf-8", newline="\n")
    result = pve.bash(
        f'set -uo pipefail\nsource "{LIB}"\nIS_TTY=1; HAS_WHIPTAIL=1\n{mode_setup}\n{extra}\n'
        'review_hub; echo "returned=$?"\n',
        extra_env={"FAKE_WT_SCRIPT": script.as_posix()},
    )
    return result, pve.log("wt.log")


def test_install_now_is_the_dialogs_own_button_not_a_row_in_the_list(pve):
    result, log = _hub(pve, ["1|"])  # the Cancel button (carrying "Install now") pressed
    assert "returned=0" in result.stdout, result.stderr
    first = log.splitlines()[0]
    assert "--cancel-button Install now" in first
    assert "--ok-button Edit (Enter)" in first
    assert ">>> Install now" not in first  # no longer an (off-screen) row
    assert "press Enter to change it" in first
    assert "press Esc to quit" in first
    assert "Advanced settings" in first


def test_the_configurator_uses_apply_changes_as_its_button(pve):
    result, log = _hub(pve, ["1|"], mode_setup="pfr_init_install; pfr_snapshot; pfr_use_config_layout")
    # nothing changed, but the screen itself must offer the button
    first = log.splitlines()[0]
    assert "--cancel-button Apply changes" in first
    assert "returned=" in result.stdout


def test_esc_on_the_review_screen_offers_to_quit(pve):
    # Esc, then "yes" to the quit question: the installer exits non-zero
    result, log = _hub(pve, ["255|", "0|"])
    assert result.returncode == 1
    assert "Cancelled" in result.stderr
    assert "Quit without installing anything?" in log


def test_declining_to_quit_returns_to_the_review_screen(pve):
    # Esc, "no" to quitting, then the proceed button
    result, _ = _hub(pve, ["255|", "1|", "1|"])
    assert "returned=0" in result.stdout, result.stderr


def test_choosing_a_setting_edits_it_and_the_list_keeps_the_selection(pve):
    result, log = _hub(pve, ["0|VLAN_TAG", "0|30", "1|"])
    assert "returned=0" in result.stdout, result.stderr
    reviews = [line for line in log.splitlines() if "--title Review" in line]
    assert len(reviews) == 2
    assert "--default-item" not in reviews[0]
    assert "--default-item VLAN_TAG" in reviews[1]


# --- temp files are always cleaned up -------------------------------------------


@pytest.mark.parametrize("ending", ["true", "exit 3", "false", "kill -TERM $$"])
def test_temp_files_registered_with_the_library_are_removed_however_the_script_ends(pve, ending):
    pve.bash(
        f'set -e\nsource "{LIB}"\npfr_mktemp a\npfr_mktemp -d 2>/dev/null; pfr_mktemp d -d\n'
        f'echo x > "$a"; touch "$d/inner"\n{ending}\n'
    )
    assert list(pve.tmpdir.iterdir()) == []


def test_a_failed_copy_out_of_a_container_leaves_no_temp_files(pve, tmp_path):
    pve.add_ct(207)
    _pull(pve, "/opt/pve-flr-portal/missing", tmp_path / "out")
    assert list(pve.tmpdir.iterdir()) == []


# --- the installer's "PVE roles to create" step ---------------------------------------


def _roles_step(pve: FakePve, answers: list[str], setup: str = "", major: int = 9):
    """Run edit_CREATE_ROLES with whiptail answering from `answers` (rc|output lines)."""
    script = pve.root / "roles_wt.txt"
    script.write_text("\n".join(answers) + "\n", encoding="utf-8", newline="\n")
    result = pve.bash(
        f'set -uo pipefail\nsource "{LIB}"\nIS_TTY=1; HAS_WHIPTAIL=1\npfr_init_install\n{setup}\n'
        'edit_CREATE_ROLES; echo "rc=$? roles=[$CREATE_ROLES]"\n',
        extra_env={"FAKE_WT_SCRIPT": script.as_posix(), "FAKE_PVE_MAJOR": str(major)},
    )
    return result, pve.log("wt.log")


def test_roles_offered_depend_on_the_pve_version(pve):
    out9 = pve.bash(f'source "{LIB}"\npfr_roles_offered | paste -sd, -\n', extra_env={"FAKE_PVE_MAJOR": "9"}).stdout
    out8 = pve.bash(f'source "{LIB}"\npfr_roles_offered | paste -sd, -\n', extra_env={"FAKE_PVE_MAJOR": "8"}).stdout
    assert out9.strip() == "FileRestoreReader,FileRestoreOperator,FileRestoreReaderHost"
    assert out8.strip() == "FileRestoreReader,FileRestoreReaderHost"  # no operator role on PVE 8


def test_the_roles_step_preselects_reader_and_operator(pve):
    result, log = _roles_step(pve, ["0|FileRestoreReader"])
    assert "rc=0 roles=[FileRestoreReader]" in result.stdout, result.stderr
    # the checklist opened with the sensible defaults ON, and the host role OFF
    assert "FileRestoreReader  ON" in log
    assert "FileRestoreOperator  ON" in log
    assert "FileRestoreReaderHost  OFF" in log
    assert "grants nothing by itself" in log


def test_the_roles_step_preselects_the_host_role_only_with_host_backup_support(pve):
    _, log = _roles_step(pve, ["0|FileRestoreReader"], setup="ENABLE_HOST_BACKUPS=true")
    assert "FileRestoreReaderHost  ON" in log


def test_the_roles_step_has_no_operator_role_on_pve_8(pve):
    result, log = _roles_step(pve, ["0|FileRestoreReader"], major=8)
    assert "FileRestoreOperator  ON" not in log
    assert "FileRestoreOperator  OFF" not in log  # no checklist entry for it (the help text still names it)
    assert "roles=[FileRestoreReader]" in result.stdout


def test_selecting_no_roles_is_allowed(pve):
    result, _ = _roles_step(pve, ["0|"])
    assert "rc=0 roles=[]" in result.stdout, result.stderr


def test_back_from_the_roles_step_changes_nothing(pve):
    result, _ = _roles_step(pve, ["1|"])
    assert "rc=1" in result.stdout


def test_an_earlier_choice_is_not_replaced_by_the_defaults_when_coming_back(pve):
    # the person chose none; going back and returning must not tick the defaults again
    result, log = _roles_step(pve, ["0|", "0|"], setup="edit_CREATE_ROLES >/dev/null; echo first=[$CREATE_ROLES]")
    assert "first=[]" in result.stdout
    second = log[log.rindex("--title PVE roles to create") :]
    assert "FileRestoreReader  OFF" in second
    assert "FileRestoreOperator  OFF" in second
    assert "  ON" not in second


def test_the_roles_row_reads_none_or_the_list(pve):
    out = pve.bash(
        f'source "{LIB}"\npfr_init_install\nshow_val CREATE_ROLES\n'
        "CREATE_ROLES=FileRestoreReader,FileRestoreOperator\nshow_val CREATE_ROLES\n"
    ).stdout.splitlines()
    assert out[0].strip() == "None"
    assert out[1].strip() == "FileRestoreReader, FileRestoreOperator"


def test_an_unknown_role_in_the_environment_is_rejected(pve):
    result = pve.bash(
        f'source "{LIB}"\nCREATE_ROLES=FileRestoreReader,Bogus\npfr_init_install\nvalidate_all; echo "rc=$?"\n',
    )
    assert "Unknown role 'Bogus'" in result.stdout
    assert "rc=1" in result.stdout


def test_the_installer_creates_the_chosen_roles_from_the_containers_checkout(pve):
    # the block at the end of lxc-create.sh, run against a container whose release has the roles tool
    import re
    import shutil

    from pve_fakes import DEPLOY as D

    text = (D / "lxc-create.sh").read_text(encoding="utf-8")
    block = re.search(r'^if \[ -n "\$CREATE_ROLES" \]; then\n.*?^fi\n', text, re.S | re.M).group(0)
    pve.add_ct(207)
    deploy = pve.ct(207) / "fs" / "opt" / "pve-flr-portal" / "deploy"
    deploy.mkdir(parents=True, exist_ok=True)
    (deploy / "host").mkdir(exist_ok=True)
    shutil.copy(D / "host" / "pfr-roles.sh", deploy / "host" / "pfr-roles.sh")
    result = pve.bash(
        f'set -uo pipefail\nsource "{LIB}"\nCTID=207\nCREATE_ROLES=FileRestoreReader,FileRestoreReaderHost\n{block}\n'
    )
    assert result.returncode == 0, result.stderr
    assert "Created FileRestoreReader" in result.stdout
    assert "Created FileRestoreReaderHost" in result.stdout
    assert set(pve.roles()) == {"FileRestoreReader", "FileRestoreReaderHost"}


def test_the_installer_says_what_to_do_when_the_release_lacks_the_roles_tool(pve):
    import re

    from pve_fakes import DEPLOY as D

    text = (D / "lxc-create.sh").read_text(encoding="utf-8")
    block = re.search(r'^if \[ -n "\$CREATE_ROLES" \]; then\n.*?^fi\n', text, re.S | re.M).group(0)
    pve.add_ct(207)  # no deploy/host/pfr-roles.sh in this container's release
    result = pve.bash(f'set -uo pipefail\nsource "{LIB}"\nCTID=207\nCREATE_ROLES=FileRestoreReader\n{block}\n')
    assert result.returncode == 0, result.stderr
    assert "doesn't include the roles tool yet" in result.stdout
    assert "pve-flr-portal roles create FileRestoreReader" in result.stdout
    assert pve.roles() == {}


def test_installing_the_host_command_from_a_container_installs_the_roles_tool_too(pve, tmp_path):
    pve.add_ct(207)
    host = pve.ct(207) / "fs" / "opt" / "pve-flr-portal" / "deploy" / "host"
    host.mkdir(parents=True)
    (host / "pve-flr-portal").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8", newline="\n")
    (host / "pfr-roles.sh").write_text("#!/usr/bin/env bash\nPFR_ROLES_SCRIPT=1\n", encoding="utf-8", newline="\n")
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    result = pve.bash(
        f'set -uo pipefail\nsource "{LIB}"\nPFR_SBIN="{sbin.as_posix()}" pfr_install_host_command 207; echo "rc=$?"\n'
    )
    assert "rc=0" in result.stdout, result.stderr
    assert (sbin / "pve-flr-portal-roles").exists()


def test_a_release_without_the_roles_tool_still_installs_the_command(pve, tmp_path):
    pve.add_ct(207)
    host = pve.ct(207) / "fs" / "opt" / "pve-flr-portal" / "deploy" / "host"
    host.mkdir(parents=True)
    (host / "pve-flr-portal").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8", newline="\n")
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    result = pve.bash(
        f'set -uo pipefail\nsource "{LIB}"\nPFR_SBIN="{sbin.as_posix()}" pfr_install_host_command 207; echo "rc=$?"\n'
    )
    assert "rc=0" in result.stdout, result.stderr
    assert (sbin / "pve-flr-portal").exists()
    assert not (sbin / "pve-flr-portal-roles").exists()


def test_the_installer_prefers_the_roles_tool_installed_on_the_host(pve, tmp_path):
    import re

    text = (DEPLOY / "lxc-create.sh").read_text(encoding="utf-8")
    block = re.search(r'^if \[ -n "\$CREATE_ROLES" \]; then\n.*?^fi\n', text, re.S | re.M).group(0)
    pve.add_ct(207)  # the container's release has no roles tool: the host's copy is what runs
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    (sbin / "pve-flr-portal-roles").write_bytes((DEPLOY / "host" / "pfr-roles.sh").read_bytes())
    result = pve.bash(
        f'set -uo pipefail\nsource "{LIB}"\nCTID=207\nCREATE_ROLES=FileRestoreReader\n'
        f'PFR_SBIN="{sbin.as_posix()}"\n{block}\n'
    )
    assert result.returncode == 0, result.stderr
    assert "Created FileRestoreReader" in result.stdout
    assert "FileRestoreReader" in pve.roles()
