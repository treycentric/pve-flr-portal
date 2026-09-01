import pytest

from backend.restore_network_pull import (
    DataNic,
    InvalidDataNicConfig,
    detect_fetch_tool,
    parse_data_nics,
    select_data_nic,
)

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
