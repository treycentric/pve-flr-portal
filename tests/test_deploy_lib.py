"""deploy/lib/settings.sh - the pieces shared by the installer and the
management tool: the container's net0 value (built and parsed), the portal's
.env (read, and patched in place), Direct Network Transfer's mapping, and the
settings registry. Run in bash against a fake `pct` (tests/fakes/pct)."""

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
