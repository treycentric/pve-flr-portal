import httpx
import respx

from backend import guest_agent
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
async def test_get_restore_capabilities_degrades_cleanly_when_agent_info_403s(session_data):
    respx.get(f"{API}/nodes/localhost/qemu/133/config").mock(
        return_value=httpx.Response(200, json={"data": {"agent": "1"}})
    )
    respx.get(f"{API}/access/permissions").mock(
        return_value=httpx.Response(200, json={"data": {"VM.GuestAgent.FileWrite": 1}})
    )
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"data": {"version": "9.2.4"}}))
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/info").mock(return_value=httpx.Response(403, text="no Audit"))
    respx.get(f"{API}/nodes/localhost/qemu/133/agent/get-osinfo").mock(return_value=httpx.Response(403))

    caps = await get_restore_capabilities(session_data, "vm", "133")
    # agent/info failed -> agent_running is False (can't confirm QGA is
    # actually responding), so nothing is offered - but no exception raised.
    assert not caps.agent_running
    assert not caps.design_a.available


@respx.mock
async def test_get_restore_capabilities_happy_path(session_data):
    respx.get(f"{API}/nodes/localhost/qemu/133/config").mock(
        return_value=httpx.Response(200, json={"data": {"agent": "1"}})
    )
    respx.get(f"{API}/access/permissions").mock(
        return_value=httpx.Response(
            200, json={"data": {"VM.GuestAgent.FileWrite": 1, "VM.GuestAgent.Unrestricted": 1}}
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
