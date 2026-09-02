import httpx
import respx

from backend import guest_agent, guest_agent_lock
from backend.guest_agent import get_restore_capabilities, parse_capabilities

API = guest_agent._API_ROOT


# --- parse_capabilities (pure logic) ------------------------------------


def test_pve8_blocks_both_designs_regardless_of_everything_else():
    caps = parse_capabilities(
        vm_config={"agent": "1"},
        agent_info={"supported_commands": [{"name": "guest-file-write", "enabled": True}]},
        permissions={"VM.GuestAgent.FileWrite": 1, "VM.GuestAgent.Unrestricted": 1},
        pve_version_major=8,
    )
    assert not caps.pve_version_ok
    assert not caps.design_a.available
    assert not caps.design_b.available
    assert "VM.Monitor" in caps.design_a.reason


def test_agent_not_running_blocks_both_designs():
    caps = parse_capabilities(
        vm_config={"agent": "0"},
        agent_info=None,
        permissions={"VM.GuestAgent.FileWrite": 1, "VM.GuestAgent.Unrestricted": 1},
        pve_version_major=9,
    )
    assert not caps.agent_running
    assert not caps.design_a.available
    assert not caps.design_b.available


def test_design_a_available_with_file_write_priv_and_running_agent():
    caps = parse_capabilities(
        vm_config={"agent": "1"},
        agent_info={"supported_commands": [{"name": "guest-file-write", "enabled": True}]},
        permissions={"VM.GuestAgent.FileWrite": 1},
        pve_version_major=9,
    )
    assert caps.design_a.available
    assert not caps.design_b.available
    assert caps.design_b.reason == "missing VM.GuestAgent.Unrestricted privilege"


def test_unrestricted_privilege_alone_covers_design_a_too():
    # Per the Proxmox privilege docs: Unrestricted covers "arbitrary"
    # commands, so it satisfies file-write's gate as well as exec's.
    caps = parse_capabilities(
        vm_config={"agent": "1"},
        agent_info={"supported_commands": [{"name": "guest-exec", "enabled": True}]},
        permissions={"VM.GuestAgent.Unrestricted": 1},
        pve_version_major=9,
    )
    assert caps.design_a.available
    assert caps.design_b.available


def test_missing_file_write_privilege_blocks_design_a_only():
    caps = parse_capabilities(
        vm_config={"agent": "1"},
        agent_info={"supported_commands": []},
        permissions={"VM.GuestAgent.Unrestricted": 1},
        pve_version_major=9,
    )
    assert caps.design_a.available  # Unrestricted covers it
    assert caps.design_b.available


def test_guest_exec_disabled_in_agent_config_blocks_design_b():
    caps = parse_capabilities(
        vm_config={"agent": "1"},
        agent_info={"supported_commands": [{"name": "guest-exec", "enabled": False}]},
        permissions={"VM.GuestAgent.Unrestricted": 1},
        pve_version_major=9,
    )
    assert not caps.design_b.available
    assert "disabled" in caps.design_b.reason


def test_file_write_disabled_in_agent_config_blocks_design_a():
    caps = parse_capabilities(
        vm_config={"agent": "1"},
        agent_info={"supported_commands": [{"name": "guest-file-write", "enabled": False}]},
        permissions={"VM.GuestAgent.FileWrite": 1},
        pve_version_major=9,
    )
    assert not caps.design_a.available


def test_verify_supported_mirrors_design_b():
    caps = parse_capabilities(
        vm_config={"agent": "1"},
        agent_info={"supported_commands": [{"name": "guest-exec", "enabled": True}]},
        permissions={"VM.GuestAgent.Unrestricted": 1},
        pve_version_major=9,
    )
    assert caps.verify_supported == caps.design_b.available


def test_unreported_command_is_treated_as_present_not_blocked():
    # supported_commands isn't guaranteed to enumerate every command on
    # every QGA version - absence shouldn't be read as "confirmed disabled".
    caps = parse_capabilities(
        vm_config={"agent": "1"},
        agent_info={"supported_commands": []},
        permissions={"VM.GuestAgent.FileWrite": 1},
        pve_version_major=9,
    )
    assert caps.design_a.available


def test_guest_os_family_detected_from_osinfo():
    caps = parse_capabilities(
        vm_config={"agent": "1"},
        agent_info={"supported_commands": []},
        permissions={},
        pve_version_major=9,
        osinfo={"id": "mswindows", "kernel-name": "windows"},
    )
    assert caps.guest_os_family == "windows"


def test_guest_os_family_unknown_when_no_osinfo():
    caps = parse_capabilities(
        vm_config={"agent": "1"}, agent_info=None, permissions={}, pve_version_major=9
    )
    assert caps.guest_os_family is None


# --- get_restore_capabilities (live orchestration, mocked httpx) --------


@respx.mock
async def test_get_restore_capabilities_degrades_cleanly_when_agent_info_403s(session_data, monkeypatch):
    monkeypatch.setattr(guest_agent_lock.asyncio, "sleep", _fake_sleep)  # skip the real retry delay
    respx.get(f"{API}/nodes/localhost/qemu/133/config").mock(
        return_value=httpx.Response(200, json={"data": {"agent": "1"}})
    )
    respx.get(f"{API}/access/permissions").mock(
        return_value=httpx.Response(200, json={"data": {"/vms/133": {"VM.GuestAgent.FileWrite": 1}}})
    )
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"data": {"version": "9.2.4"}}))
    # 403 on every attempt (including the retry) - genuinely unavailable.
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/info").mock(return_value=httpx.Response(403, text="no Audit"))
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/get-osinfo").mock(return_value=httpx.Response(403))

    caps = await get_restore_capabilities(session_data, "vm", "133")
    # agent/info failed -> agent_running is False (can't confirm QGA is
    # actually responding), so nothing is offered - but no exception raised.
    assert not caps.agent_running
    assert not caps.design_a.available


async def _fake_sleep(*_args, **_kwargs):
    return None


@respx.mock
async def test_get_restore_capabilities_retries_agent_info_once_on_definite_http_error(session_data, monkeypatch):
    # Retry only applies to a *completed* bad response - PVE/QEMU already
    # finished handling it, so a second attempt is safe.
    monkeypatch.setattr(guest_agent_lock.asyncio, "sleep", _fake_sleep)
    respx.get(f"{API}/nodes/localhost/qemu/133/config").mock(
        return_value=httpx.Response(200, json={"data": {"agent": "1"}})
    )
    respx.get(f"{API}/access/permissions").mock(
        return_value=httpx.Response(200, json={"data": {"/vms/133": {"VM.GuestAgent.FileWrite": 1}}})
    )
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"data": {"version": "9.2.4"}}))
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/info").mock(
        side_effect=[
            httpx.Response(500, text="temporary error"),
            httpx.Response(200, json={"data": {"supported_commands": []}}),
        ]
    )
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/get-osinfo").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )

    caps = await get_restore_capabilities(session_data, "vm", "133")
    assert caps.agent_running  # the retry succeeded, so this must not read as "not running"


@respx.mock
async def test_get_restore_capabilities_does_not_retry_after_a_timeout(session_data):
    # A timeout is ambiguous - qemu-guest-agent only accepts one command
    # at a time over its virtio-serial channel, and a client-side timeout
    # doesn't prove the in-guest command was actually abandoned. Sending
    # a second one anyway risks desyncing the channel, so this must NOT
    # retry - one attempt, then a clean "unavailable".
    respx.get(f"{API}/nodes/localhost/qemu/133/config").mock(
        return_value=httpx.Response(200, json={"data": {"agent": "1"}})
    )
    respx.get(f"{API}/access/permissions").mock(
        return_value=httpx.Response(200, json={"data": {"/vms/133": {"VM.GuestAgent.FileWrite": 1}}})
    )
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"data": {"version": "9.2.4"}}))
    route = respx.get(f"{API}/nodes/localhost/qemu/133/agent/info").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/get-osinfo").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )

    caps = await get_restore_capabilities(session_data, "vm", "133")
    assert not caps.agent_running
    assert route.call_count == 1  # no retry after an ambiguous failure


@respx.mock
async def test_get_restore_capabilities_does_not_retry_get_osinfo(session_data):
    # get-osinfo only feeds guest_os_family (a UI nicety), not the
    # availability gate itself - one attempt is enough, no retry needed.
    respx.get(f"{API}/nodes/localhost/qemu/133/config").mock(
        return_value=httpx.Response(200, json={"data": {"agent": "1"}})
    )
    respx.get(f"{API}/access/permissions").mock(
        return_value=httpx.Response(200, json={"data": {"/vms/133": {"VM.GuestAgent.FileWrite": 1}}})
    )
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"data": {"version": "9.2.4"}}))
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/info").mock(
        return_value=httpx.Response(200, json={"data": {"supported_commands": []}})
    )
    route = respx.get(f"{API}/nodes/localhost/qemu/133/agent/get-osinfo").mock(return_value=httpx.Response(500))

    caps = await get_restore_capabilities(session_data, "vm", "133")
    assert caps.guest_os_family is None
    assert route.call_count == 1


@respx.mock
async def test_get_restore_capabilities_happy_path(session_data):
    respx.get(f"{API}/nodes/localhost/qemu/133/config").mock(
        return_value=httpx.Response(200, json={"data": {"agent": "1"}})
    )
    respx.get(f"{API}/access/permissions").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"/vms/133": {"VM.GuestAgent.FileWrite": 1, "VM.GuestAgent.Unrestricted": 1}}},
        )
    )
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"data": {"version": "9.2.4"}}))
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/info").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "supported_commands": [
                        {"name": "guest-file-write", "enabled": True},
                        {"name": "guest-exec", "enabled": True},
                    ]
                }
            },
        )
    )
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/get-osinfo").mock(
        return_value=httpx.Response(200, json={"data": {"result": {"id": "debian", "kernel-name": "linux"}}})
    )

    caps = await get_restore_capabilities(session_data, "vm", "133")
    assert caps.agent_running
    assert caps.pve_version_ok
    assert caps.design_a.available
    assert caps.design_b.available
    assert caps.guest_os_family == "linux"


@respx.mock
async def test_get_restore_capabilities_lxc_skips_agent_calls(session_data):
    respx.get(f"{API}/nodes/localhost/lxc/133/config").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )
    respx.get(f"{API}/access/permissions").mock(return_value=httpx.Response(200, json={"data": {}}))
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"data": {"version": "9.2.4"}}))

    caps = await get_restore_capabilities(session_data, "ct", "133")
    assert not caps.agent_running
    assert not caps.design_a.available


@respx.mock
async def test_get_restore_capabilities_unwraps_permissions_nested_under_path(session_data):
    # Regression test: /access/permissions?path=/vms/{vmid} nests the
    # privilege dict under the path key itself - confirmed live against a
    # real guest (docs/plan.md §7.5) - not a flat {"VM.GuestAgent...": 1}.
    # A response that also carries an unrelated path must not leak its
    # privileges onto this guest either.
    respx.get(f"{API}/nodes/localhost/qemu/133/config").mock(
        return_value=httpx.Response(200, json={"data": {"agent": "1"}})
    )
    respx.get(f"{API}/access/permissions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "/vms/999": {"VM.GuestAgent.Unrestricted": 1},  # a different guest - must not leak in
                    "/vms/133": {"VM.GuestAgent.FileWrite": 1},
                }
            },
        )
    )
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"data": {"version": "9.2.4"}}))
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/info").mock(
        return_value=httpx.Response(200, json={"data": {"supported_commands": []}})
    )
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/get-osinfo").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )

    caps = await get_restore_capabilities(session_data, "vm", "133")
    assert caps.design_a.available  # has FileWrite on /vms/133
    assert not caps.design_b.available  # Unrestricted was only granted on /vms/999


@respx.mock
async def test_get_restore_capabilities_missing_path_key_means_no_privileges(session_data):
    respx.get(f"{API}/nodes/localhost/qemu/133/config").mock(
        return_value=httpx.Response(200, json={"data": {"agent": "1"}})
    )
    respx.get(f"{API}/access/permissions").mock(return_value=httpx.Response(200, json={"data": {}}))
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"data": {"version": "9.2.4"}}))
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/info").mock(
        return_value=httpx.Response(200, json={"data": {"supported_commands": []}})
    )
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/get-osinfo").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )

    caps = await get_restore_capabilities(session_data, "vm", "133")
    assert not caps.design_a.available
    assert not caps.design_b.available


# --- get_guest_ip_addresses (Design C, docs/plan.md §7.6, issue #22) ----


def test_extract_ip_addresses_flattens_and_skips_loopback():
    interfaces = [
        {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}]},
        {
            "name": "eth0",
            "ip-addresses": [
                {"ip-address": "10.0.5.42", "ip-address-type": "ipv4"},
                {"ip-address": "fe80::1", "ip-address-type": "ipv6"},
            ],
        },
    ]
    assert guest_agent._extract_ip_addresses(interfaces) == ["10.0.5.42", "fe80::1"]


def test_extract_ip_addresses_tolerates_none_or_empty():
    assert guest_agent._extract_ip_addresses(None) == []
    assert guest_agent._extract_ip_addresses([]) == []
    assert guest_agent._extract_ip_addresses([{"name": "eth0"}]) == []


@respx.mock
async def test_get_guest_ip_addresses_happy_path(session_data):
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/network-get-interfaces").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "result": [
                        {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
                        {"name": "eth0", "ip-addresses": [{"ip-address": "10.0.5.42"}]},
                    ]
                }
            },
        )
    )
    assert await guest_agent.get_guest_ip_addresses(session_data, "vm", "133") == ["10.0.5.42"]


async def test_get_guest_ip_addresses_lxc_returns_empty_without_any_call(session_data, monkeypatch):
    async def fail_if_called(*a, **kw):
        raise AssertionError("LXC containers have no qemu-guest-agent - should never be called")

    monkeypatch.setattr(guest_agent, "_get_agent_json", fail_if_called)
    assert await guest_agent.get_guest_ip_addresses(session_data, "ct", "133") == []


@respx.mock
async def test_get_guest_ip_addresses_degrades_to_empty_on_failure(session_data):
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/network-get-interfaces").mock(return_value=httpx.Response(403))
    assert await guest_agent.get_guest_ip_addresses(session_data, "vm", "133") == []
