"""deploy/pfr-manage.sh - what `pve-flr-portal config` does once the person has
chosen their changes: reading the container's current state, listing what
will change, applying network changes (with rollback), and patching .env.

The interactive screens can't be driven here; these source the script and call
its functions against a fake `pct` (tests/fakes/pct)."""

import pytest
from pve_fakes import DEPLOY, FakePve

MANAGE = (DEPLOY / "pfr-manage.sh").as_posix()


@pytest.fixture
def pve():
    fake = FakePve()
    yield fake
    fake.close()


def run(pve: FakePve, body: str, stdin: str | None = None, check: bool = True):
    result = pve.bash(f'set -euo pipefail\nsource "{MANAGE}"\n{body}\n', stdin=stdin)
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


LOAD = "PFR_MODE=config; load_current 207; pfr_use_config_layout\n"


def test_load_current_reads_the_network_and_the_env(pve):
    pve.add_ct(
        207,
        name="flr",
        net0="name=eth0,bridge=vmbr20,hwaddr=BC:24:11:AA:BB:CC,tag=100,ip=10.0.0.5/24,gw=10.0.0.1,ip6=auto,type=veth",
        extra="nameserver: 1.1.1.1 9.9.9.9\nsearchdomain: example.com\n",
    )
    out = (
        run(
            pve,
            LOAD + 'echo "$CT_HOSTNAME|$BRIDGE|$VLAN_TAG|$USE_STATIC_IP|$STATIC_CIDR|$STATIC_GW|$IPV6_MODE|'
            "$DNS_SERVERS|$SEARCH_DOMAIN|"
            '$PVE_HOST|$PVE_STORAGE|$ENABLE_DNT|$DNT_LOCAL_IP|$OLD_ADDR"',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert out == (
        "flr|vmbr20|100|yes|10.0.0.5/24|10.0.0.1|auto|1.1.1.1 9.9.9.9|example.com|"
        "titan.example.net|pbs-a|yes|192.168.1.50|192.168.1.0/24 192.168.1.50"
    )


def test_ipv6_with_no_ip6_field_reads_as_disabled_only_when_the_marker_exists(pve):
    pve.add_ct(207)
    out = run(pve, LOAD + 'echo "$IPV6_MODE"').stdout.strip().splitlines()[-1]
    assert out == "none"  # unmanaged: no ip6 and no marker
    marker = pve.ct(207) / "fs" / "etc" / "sysctl.d"
    marker.mkdir(parents=True)
    (marker / "99-disable-ipv6.conf").write_text("x\n", encoding="utf-8")
    out = run(pve, LOAD + 'echo "$IPV6_MODE"').stdout.strip().splitlines()[-1]
    assert out == "disabled"


def test_pending_summary_lists_old_and_new_values(pve):
    pve.add_ct(207)
    out = run(pve, LOAD + 'DEFAULT_THEME=dark; BRIDGE=vmbr5; printf "%s" "$(pending_summary)"').stdout
    assert "- Default colour theme: Follow the browser/OS -> Dark" in out
    assert "- Network bridge: vmbr0 -> vmbr5" in out


def test_nothing_to_apply_returns_to_the_review_screen(pve):
    pve.add_ct(207)
    result = run(pve, LOAD + "if apply_changes 207; then echo APPLIED; else echo BACK; fi")
    assert "BACK" in result.stdout
    assert "Nothing to apply" in result.stdout
    assert pve.log("set.log") == ""


def test_declining_the_confirmation_changes_nothing(pve):
    pve.add_ct(207)
    result = run(
        pve, LOAD + "DEFAULT_THEME=dark; if apply_changes 207; then echo APPLIED; else echo BACK; fi", stdin="n\n"
    )
    assert "BACK" in result.stdout
    assert "DEFAULT_THEME=auto" in pve.read(207, "/opt/pve-flr-portal/.env")


def test_env_only_changes_write_env_and_restart_the_service_without_touching_the_network(pve):
    pve.add_ct(207)
    result = run(
        pve,
        LOAD + "DEFAULT_THEME=dark; JOB_HISTORY_RETENTION_DAYS=30\n"
        "PFR_ENV_FILE=/opt/pve-flr-portal/.env PFR_OVERRIDES_TMP=/tmp/pfr-overrides.env\n"
        "if apply_changes 207; then echo APPLIED; fi",
        stdin="y\n",
    )
    assert "APPLIED" in result.stdout
    env = pve.read(207, "/opt/pve-flr-portal/.env")
    assert "DEFAULT_THEME=dark" in env
    assert "JOB_HISTORY_RETENTION_DAYS=30" in env
    assert "systemctl restart pve-flr-portal" in pve.log("exec.log")
    assert "reboot" not in pve.log("set.log")  # no network change, no restart of the container
    # a hand-formatted data network is not rewritten just because something else changed
    assert 'RESTORE_DATA_NICS=[{"cidr": "192.168.1.0/24", "local_ip": "192.168.1.50"}]' in env


def test_network_change_preserves_the_mac_and_other_fields_and_reboots(pve):
    pve.add_ct(207, net0="name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:CC,firewall=1,ip=dhcp,type=veth")
    result = run(
        pve,
        LOAD + "VLAN_TAG=30; USE_STATIC_IP=yes; STATIC_CIDR=10.9.0.5/24; STATIC_GW=10.9.0.1;"
        ' DNS_SERVERS="1.1.1.1, 9.9.9.9"\n'
        "apply_network 207 && echo NET_OK",
    )
    assert "NET_OK" in result.stdout
    config = pve.config(207)
    net0 = next(line for line in config.splitlines() if line.startswith("net0:"))
    for kept in ("hwaddr=BC:24:11:AA:BB:CC", "firewall=1", "type=veth"):
        assert kept in net0
    assert "tag=30" in net0
    assert "ip=10.9.0.5/24" in net0 and "gw=10.9.0.1" in net0
    assert "nameserver: 1.1.1.1 9.9.9.9" in config
    assert "reboot 207" in pve.log("set.log")


def test_blanked_dns_is_deleted_so_the_host_settings_apply_again(pve):
    pve.add_ct(207, extra="nameserver: 1.1.1.1\nsearchdomain: example.com\n")
    run(pve, LOAD + 'DNS_SERVERS=""; SEARCH_DOMAIN=""; apply_network 207')
    config = pve.config(207)
    assert "nameserver" not in config
    assert "searchdomain" not in config
    assert "--delete nameserver" in pve.log("set.log")


def test_an_unreachable_container_gets_its_previous_network_back(pve):
    original = "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:CC,ip=dhcp,type=veth"
    pve.add_ct(207, net0=original)
    (pve.ct(207) / "bad_net").write_text("bridge=vmbr9\n", encoding="utf-8", newline="\n")
    result = run(
        pve,
        LOAD
        + 'BRIDGE=vmbr9\nif apply_network 207; then echo NET_OK; else echo ROLLED_BACK; echo "bridge now: $BRIDGE"; fi',
    )
    assert "ROLLED_BACK" in result.stdout
    assert f"net0: {original}" in pve.config(207)
    assert "bridge now: vmbr9" in result.stdout  # the person's attempted value is kept for them to adjust
    assert pve.log("set.log").count("reboot 207") == 2  # once to try, once to restore


def test_proxmox_rejecting_the_change_leaves_everything_alone(pve):
    pve.add_ct(207)
    # an option the fake rejects, as `pct set` would reject an invalid value
    result = run(
        pve,
        LOAD + 'PVE_HOSTNAME_BAD=1; net_set_args apply "x" "$CT_HOSTNAME" "" "" "" ""; NET_ARGS+=(--bogus 1)\n'
        'if pct set 207 "${NET_ARGS[@]}"; then echo SET_OK; else echo SET_FAILED; fi',
        check=False,
    )
    assert "SET_FAILED" in result.stdout


def test_data_network_follows_the_container_when_its_address_was_detected(pve):
    pve.add_ct(207, addr="192.168.1.50/24")
    result = run(
        pve,
        LOAD + "USE_STATIC_IP=yes; STATIC_CIDR=10.9.0.5/24; STATIC_GW=10.9.0.1\n"
        "PFR_ENV_FILE=/opt/pve-flr-portal/.env PFR_OVERRIDES_TMP=/tmp/pfr-overrides.env\n"
        "apply_network 207 && apply_env 207 && echo ALL_OK",
    )
    assert "ALL_OK" in result.stdout
    env = pve.read(207, "/opt/pve-flr-portal/.env")
    assert 'RESTORE_DATA_NICS=[{"cidr":"10.9.0.0/24","local_ip":"10.9.0.5"}]' in env


def test_a_data_network_entered_by_hand_is_kept_and_flagged(pve):
    pve.add_ct(
        207,
        addr="192.168.1.50/24",
        env_text='PVE_HOST=h\nRESTORE_DATA_NICS=[{"cidr":"10.5.0.0/24","local_ip":"10.5.0.9"}]\n',
    )
    result = run(
        pve,
        LOAD + "USE_STATIC_IP=yes; STATIC_CIDR=10.9.0.5/24; STATIC_GW=10.9.0.1\n"
        "PFR_ENV_FILE=/opt/pve-flr-portal/.env PFR_OVERRIDES_TMP=/tmp/pfr-overrides.env\n"
        "apply_network 207 && apply_env 207 && echo ALL_OK",
    )
    assert "ALL_OK" in result.stdout
    assert "10.5.0.9" in pve.read(207, "/opt/pve-flr-portal/.env")
    assert "not the container's address" in result.stdout + result.stderr


def test_turning_data_network_transfer_off_writes_an_empty_list(pve):
    pve.add_ct(207)
    run(
        pve,
        LOAD + "ENABLE_DNT=no\n"
        "PFR_ENV_FILE=/opt/pve-flr-portal/.env PFR_OVERRIDES_TMP=/tmp/pfr-overrides.env\n"
        "apply_env 207",
    )
    assert "RESTORE_DATA_NICS=[]" in pve.read(207, "/opt/pve-flr-portal/.env")


def test_several_data_networks_are_left_alone(pve):
    two = '[{"cidr":"10.1.0.0/24","local_ip":"10.1.0.2"},{"cidr":"10.2.0.0/24","local_ip":"10.2.0.2"}]'
    pve.add_ct(207, env_text=f"PVE_HOST=h\nDEFAULT_THEME=auto\nRESTORE_DATA_NICS={two}\n")
    run(
        pve,
        LOAD + "DEFAULT_THEME=dark\n"
        "PFR_ENV_FILE=/opt/pve-flr-portal/.env PFR_OVERRIDES_TMP=/tmp/pfr-overrides.env\n"
        "apply_env 207",
    )
    env = pve.read(207, "/opt/pve-flr-portal/.env")
    assert f"RESTORE_DATA_NICS={two}" in env
    assert "DEFAULT_THEME=dark" in env


def test_config_refuses_without_a_terminal_and_with_a_bad_container_id(pve):
    pve.add_ct(207)
    script = DEPLOY / "pfr-manage.sh"
    result = pve.run_script(script, ["config", "notanumber"])
    assert result.returncode == 2
    result = pve.run_script(script, ["bogus", "207"])
    assert result.returncode == 2
    result = pve.run_script(script, ["--help"])
    assert result.returncode == 0 and "Usage: pfr-manage.sh config CTID" in result.stdout


def test_the_welcome_screen_names_the_container_being_configured(pve):
    pve.add_ct(207, name="flr-prod")
    run(pve, LOAD + "IS_TTY=1; HAS_WHIPTAIL=1; set_welcome 207; show_welcome")
    log = pve.log("wt.log")
    assert "Welcome to the pve-flr-portal configurator" in log
    assert "container 207 (flr-prod)" in log
    assert "Nothing is applied until you choose" in log
