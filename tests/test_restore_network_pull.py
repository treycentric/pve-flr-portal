import pytest

from backend.restore_network_pull import (
    DataNic,
    InvalidDataNicConfig,
    build_fetch_command,
    detect_fetch_tool,
    parse_data_nics,
    select_data_nic,
)

URL = "https://10.0.5.5:8008/api/restore-downloads/abc123"

# --- parse_data_nics --------------------------------------------------

def test_parse_data_nics_empty_or_blank_means_unconfigured():
    assert parse_data_nics("") == []
    assert parse_data_nics("   ") == []
    assert parse_data_nics("[]") == []


def test_parse_data_nics_parses_a_valid_list():
    raw = '[{"cidr": "10.0.5.0/24", "local_ip": "10.0.5.5"}, {"cidr": "10.0.6.0/24", "local_ip": "10.0.6.5"}]'
    nics = parse_data_nics(raw)
    assert nics == [DataNic("10.0.5.0/24", "10.0.5.5"), DataNic("10.0.6.0/24", "10.0.6.5")]


def test_parse_data_nics_rejects_invalid_json():
    with pytest.raises(InvalidDataNicConfig):
        parse_data_nics("not json")


def test_parse_data_nics_rejects_non_array():
    with pytest.raises(InvalidDataNicConfig):
        parse_data_nics('{"cidr": "10.0.5.0/24", "local_ip": "10.0.5.5"}')


def test_parse_data_nics_rejects_missing_fields():
    with pytest.raises(InvalidDataNicConfig):
        parse_data_nics('[{"cidr": "10.0.5.0/24"}]')


def test_parse_data_nics_rejects_malformed_cidr_or_ip():
    with pytest.raises(InvalidDataNicConfig):
        parse_data_nics('[{"cidr": "not-a-subnet", "local_ip": "10.0.5.5"}]')
    with pytest.raises(InvalidDataNicConfig):
        parse_data_nics('[{"cidr": "10.0.5.0/24", "local_ip": "not-an-ip"}]')


# --- select_data_nic ----------------------------------------------------

def test_select_data_nic_matches_the_subnet_containing_a_guest_ip():
    nics = [DataNic("10.0.5.0/24", "10.0.5.5"), DataNic("10.0.6.0/24", "10.0.6.5")]
    assert select_data_nic(["10.0.6.42"], nics) == nics[1]


def test_select_data_nic_returns_none_when_nothing_matches():
    nics = [DataNic("10.0.5.0/24", "10.0.5.5")]
    assert select_data_nic(["192.168.1.10"], nics) is None


def test_select_data_nic_returns_none_with_no_configured_nics():
    assert select_data_nic(["10.0.5.42"], []) is None


def test_select_data_nic_skips_unparsable_guest_addresses_without_erroring():
    nics = [DataNic("10.0.5.0/24", "10.0.5.5")]
    # A malformed entry (e.g. QGA reporting something odd) shouldn't blow
    # up matching against the rest of the list.
    assert select_data_nic(["not-an-ip", "10.0.5.42"], nics) == nics[0]


def test_select_data_nic_first_match_wins_when_multiple_nics_could_match():
    nics = [DataNic("10.0.0.0/8", "10.0.0.1"), DataNic("10.0.5.0/24", "10.0.5.5")]
    assert select_data_nic(["10.0.5.42"], nics) == nics[0]


# --- detect_fetch_tool ---------------------------------------------------

def _exec_returning(results: dict[str, tuple[int, str, str]]):
    """Fake exec_fn: looks up a canned result by the probe's first argv
    element (good enough to distinguish candidates in these tests)."""

    async def fake(argv):
        key = argv[0]
        if key not in results:
            raise AssertionError(f"unexpected probe: {argv}")
        return results[key]

    return fake


async def test_detect_fetch_tool_windows_prefers_invoke_webrequest_when_present():
    fake = _exec_returning({"powershell": (0, "", "")})
    assert await detect_fetch_tool(fake, "windows") == "Invoke-WebRequest"


async def test_detect_fetch_tool_windows_falls_back_down_the_list():
    fake = _exec_returning({"powershell": (1, "", "not found"), "where": (0, "", "")})
    assert await detect_fetch_tool(fake, "windows") == "certutil"


async def test_detect_fetch_tool_linux_prefers_curl():
    fake = _exec_returning({"sh": (0, "/usr/bin/curl", "")})
    assert await detect_fetch_tool(fake, "linux") == "curl"


async def test_detect_fetch_tool_returns_none_when_nothing_is_available():
    async def always_fails(argv):
        return 1, "", "not found"

    assert await detect_fetch_tool(always_fails, "linux") is None
    assert await detect_fetch_tool(always_fails, "windows") is None


async def test_detect_fetch_tool_returns_none_for_unknown_os_family():
    async def fail_if_called(argv):
        raise AssertionError("should never probe with an unknown OS family")

    assert await detect_fetch_tool(fail_if_called, None) is None


async def test_detect_fetch_tool_tolerates_a_probe_raising_and_tries_the_next_one():
    calls = []

    async def flaky(argv):
        calls.append(argv[0])
        if argv[0] == "powershell":
            raise TimeoutError("guest-exec timed out")
        return 0, "", ""

    assert await detect_fetch_tool(flaky, "windows") == "certutil"
    assert calls == ["powershell", "where"]


# --- build_fetch_command --------------------------------------------------

DEST_WIN = "C:\\Windows\\Temp\\hosts"
DEST_POSIX = "/etc/hosts"


def test_build_fetch_command_invoke_webrequest():
    plan = build_fetch_command("Invoke-WebRequest", URL, DEST_WIN, "windows")
    assert plan.stage_content is None
    assert plan.exec_argv[0] == "powershell"
    script = plan.exec_argv[-1]
    assert URL in script and DEST_WIN in script
    assert "Invoke-WebRequest" in script


def test_build_fetch_command_certutil():
    plan = build_fetch_command("certutil", URL, DEST_WIN, "windows")
    assert plan.exec_argv == ["certutil", "-urlcache", "-split", "-f", URL, DEST_WIN]
    assert plan.stage_content is None


def test_build_fetch_command_bitsadmin():
    # Plain argv, not cmd /c - confirmed live 2026-09-01/2026-09-02 that
    # cmd.exe's handling of multiple embedded double-quoted segments on
    # one /c line is unreliable, even against perfectly valid paths.
    plan = build_fetch_command("bitsadmin", URL, DEST_WIN, "windows")
    assert plan.exec_argv[0] == "bitsadmin"
    assert "cmd" not in plan.exec_argv
    assert URL in plan.exec_argv
    assert DEST_WIN in plan.exec_argv


def test_build_fetch_command_cscript_requires_a_stage_path():
    with pytest.raises(ValueError):
        build_fetch_command("cscript", URL, DEST_WIN, "windows")


def test_build_fetch_command_cscript_stages_a_vbs_script():
    plan = build_fetch_command("cscript", URL, DEST_WIN, "windows", stage_path="C:\\Windows\\Temp\\x.vbs")
    assert plan.exec_argv == ["cscript", "//nologo", "//B", "C:\\Windows\\Temp\\x.vbs"]
    assert plan.stage_path == "C:\\Windows\\Temp\\x.vbs"
    assert URL in plan.stage_content
    assert DEST_WIN in plan.stage_content
    assert "WinHttp.WinHttpRequest" in plan.stage_content


def test_build_fetch_command_curl():
    plan = build_fetch_command("curl", URL, DEST_POSIX, "linux")
    assert plan.exec_argv == ["curl", "-fsSL", "-o", DEST_POSIX, URL]


def test_build_fetch_command_wget():
    plan = build_fetch_command("wget", URL, DEST_POSIX, "linux")
    assert plan.exec_argv == ["wget", "-q", "-O", DEST_POSIX, URL]


def test_build_fetch_command_python():
    plan = build_fetch_command("python3", URL, DEST_POSIX, "linux")
    assert plan.exec_argv[0] == "python3"
    assert URL in plan.exec_argv[-1] and DEST_POSIX in plan.exec_argv[-1]
    assert "urlretrieve" in plan.exec_argv[-1]


def test_build_fetch_command_bash_devtcp_over_plain_http():
    plan = build_fetch_command("bash", "http://10.0.5.5:8008/api/restore-downloads/abc123", DEST_POSIX, "linux")
    assert plan.exec_argv[0] == "bash"
    script = plan.exec_argv[-1]
    assert "/dev/tcp/10.0.5.5/8008" in script
    assert DEST_POSIX in script


def test_build_fetch_command_bash_devtcp_rejects_https_url():
    # /dev/tcp is a plain socket - bash has no TLS, so an https URL would
    # fail confusingly in the guest rather than cleanly here.
    with pytest.raises(ValueError):
        build_fetch_command("bash", URL, DEST_POSIX, "linux")  # URL is https://


def test_build_fetch_command_unknown_tool_raises():
    with pytest.raises(ValueError):
        build_fetch_command("magic", URL, DEST_POSIX, "linux")
