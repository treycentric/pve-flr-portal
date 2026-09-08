import pytest

from backend.restore_network_pull import (
    DataNic,
    InvalidDataNicConfig,
    build_fetch_command,
    detect_fetch_tool,
    parse_data_nics,
    resolve_tls_mode,
    select_data_nic,
    tool_supports_tls,
)

HTTPS_URL = "https://10.0.5.5:8008/api/restore-downloads/abc123"
HTTP_URL = "http://10.0.5.5:8008/api/restore-downloads/abc123"
URL = HTTPS_URL  # back-compat alias for the tests below

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


def test_parse_data_nics_reads_optional_per_nic_hostname():
    nics = parse_data_nics(
        '[{"cidr": "10.0.5.0/24", "local_ip": "10.0.5.5", "hostname": "restore.dc1.lan"},'
        ' {"cidr": "10.0.6.0/24", "local_ip": "10.0.6.5"}]'
    )
    assert nics[0].hostname == "restore.dc1.lan"
    assert nics[0].url_host == "restore.dc1.lan"
    assert nics[1].hostname is None
    assert nics[1].url_host == "10.0.6.5"


def test_parse_data_nics_rejects_blank_hostname():
    with pytest.raises(InvalidDataNicConfig):
        parse_data_nics('[{"cidr": "10.0.5.0/24", "local_ip": "10.0.5.5", "hostname": "  "}]')


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


# --- data-plane TLS ladder (issue #47) ----------------------------------

def test_tool_supports_tls_capability_matrix():
    for tool in ("curl", "wget", "python3", "Invoke-WebRequest", "cscript", "certutil", "bitsadmin", "bash"):
        assert tool_supports_tls(tool, "plaintext")
    for tool in ("curl", "wget", "python3", "Invoke-WebRequest", "cscript"):
        assert tool_supports_tls(tool, "insecure")
    for tool in ("certutil", "bitsadmin", "bash"):
        assert not tool_supports_tls(tool, "insecure")
    for tool in ("curl", "certutil", "bitsadmin", "Invoke-WebRequest"):
        assert tool_supports_tls(tool, "verify")
    assert not tool_supports_tls("bash", "verify")


def test_is_tls_negotiation_failure():
    from backend.restore_network_pull import is_tls_negotiation_failure

    assert is_tls_negotiation_failure("curl", 60, "curl: (60) SSL certificate problem: self-signed certificate")
    assert is_tls_negotiation_failure("curl", 35, "")  # SSL connect error by exit code
    assert is_tls_negotiation_failure("wget", 5, "")
    assert is_tls_negotiation_failure(
        "Invoke-WebRequest", 1, "Could not establish trust relationship for the SSL/TLS secure channel"
    )
    assert not is_tls_negotiation_failure("curl", 0, "")  # success
    assert not is_tls_negotiation_failure("curl", 7, "curl: (7) Failed to connect")  # plain connect failure
    assert not is_tls_negotiation_failure("curl", 23, "curl: (23) Failure writing output")  # mid-transfer


def test_resolve_tls_mode_walks_the_ladder_down_from_preferred():
    assert resolve_tls_mode("curl", "verify", "insecure") == "verify"
    # certutil can't skip-verify, so verify is the only rung it can do
    assert resolve_tls_mode("certutil", "verify", "insecure") == "verify"
    # ...and if preferred=insecure with the same floor, nothing qualifies
    assert resolve_tls_mode("certutil", "insecure", "insecure") is None
    # bash: only plaintext, so only reachable if the floor allows it
    assert resolve_tls_mode("bash", "verify", "plaintext") == "plaintext"
    assert resolve_tls_mode("bash", "verify", "insecure") is None


# --- build_fetch_command --------------------------------------------------

DEST_WIN = "C:\\Windows\\Temp\\hosts"
DEST_POSIX = "/etc/hosts"


def test_build_fetch_command_rejects_scheme_mismatch():
    with pytest.raises(ValueError):
        build_fetch_command("curl", HTTPS_URL, DEST_POSIX, "linux", tls="plaintext")
    with pytest.raises(ValueError):
        build_fetch_command("curl", HTTP_URL, DEST_POSIX, "linux", tls="verify")


def test_build_fetch_command_invoke_webrequest_verify_and_insecure():
    verify = build_fetch_command("Invoke-WebRequest", HTTPS_URL, DEST_WIN, "windows", tls="verify")
    vscript = verify.exec_argv[-1]
    assert "Invoke-WebRequest" in vscript and HTTPS_URL in vscript and DEST_WIN in vscript
    assert "Tls12" in vscript
    assert "ServerCertificateValidationCallback" not in vscript

    insec = build_fetch_command("Invoke-WebRequest", HTTPS_URL, DEST_WIN, "windows", tls="insecure")
    assert "ServerCertificateValidationCallback = { $true }" in insec.exec_argv[-1]


def test_build_fetch_command_certutil_bitsadmin_verify_ok_insecure_raises():
    for tool in ("certutil", "bitsadmin"):
        plan = build_fetch_command(tool, HTTPS_URL, DEST_WIN, "windows", tls="verify")
        assert plan.exec_argv[0] == tool
        assert HTTPS_URL in plan.exec_argv and DEST_WIN in plan.exec_argv
        with pytest.raises(ValueError, match="skip TLS"):
            build_fetch_command(tool, HTTPS_URL, DEST_WIN, "windows", tls="insecure")


def test_build_fetch_command_cscript_requires_a_stage_path():
    with pytest.raises(ValueError):
        build_fetch_command("cscript", HTTPS_URL, DEST_WIN, "windows", tls="verify")


def test_build_fetch_command_cscript_insecure_sets_ssl_ignore_option():
    verify = build_fetch_command(
        "cscript", HTTPS_URL, DEST_WIN, "windows", stage_path="C:\\Windows\\Temp\\x.vbs", tls="verify"
    )
    assert "http.Option(4)" not in verify.stage_content
    assert "WinHttp.WinHttpRequest" in verify.stage_content

    insec = build_fetch_command(
        "cscript", HTTPS_URL, DEST_WIN, "windows", stage_path="C:\\Windows\\Temp\\x.vbs", tls="insecure"
    )
    assert "http.Option(4) = 13056" in insec.stage_content


def test_build_fetch_command_curl_plaintext_and_insecure():
    assert build_fetch_command("curl", HTTP_URL, DEST_POSIX, "linux", tls="plaintext").exec_argv == [
        "curl", "-fsSL", "-o", DEST_POSIX, HTTP_URL,
    ]
    assert build_fetch_command("curl", HTTPS_URL, DEST_POSIX, "linux", tls="insecure").exec_argv == [
        "curl", "-fsSL", "-k", "-o", DEST_POSIX, HTTPS_URL,
    ]
    assert "-k" not in build_fetch_command("curl", HTTPS_URL, DEST_POSIX, "linux", tls="verify").exec_argv


def test_build_fetch_command_wget_insecure_adds_no_check_certificate():
    assert build_fetch_command("wget", HTTPS_URL, DEST_POSIX, "linux", tls="insecure").exec_argv == [
        "wget", "-q", "--no-check-certificate", "-O", DEST_POSIX, HTTPS_URL,
    ]
    assert "--no-check-certificate" not in build_fetch_command(
        "wget", HTTPS_URL, DEST_POSIX, "linux", tls="verify"
    ).exec_argv


def test_build_fetch_command_python_streams_and_threads_an_unverified_context():
    verify = build_fetch_command("python3", HTTPS_URL, DEST_POSIX, "linux", tls="verify").exec_argv[-1]
    assert "copyfileobj" in verify and "urlretrieve" not in verify
    assert "_create_unverified_context" not in verify

    insec = build_fetch_command("python3", HTTPS_URL, DEST_POSIX, "linux", tls="insecure").exec_argv[-1]
    assert "_create_unverified_context" in insec and "context=ctx" in insec


def test_build_fetch_command_bash_devtcp_over_plain_http():
    plan = build_fetch_command("bash", HTTP_URL, DEST_POSIX, "linux", tls="plaintext")
    assert plan.exec_argv[0] == "bash"
    script = plan.exec_argv[-1]
    assert "/dev/tcp/10.0.5.5/8008" in script
    assert DEST_POSIX in script


def test_build_fetch_command_bash_rejects_any_tls():
    with pytest.raises(ValueError):
        build_fetch_command("bash", HTTPS_URL, DEST_POSIX, "linux", tls="insecure")
    with pytest.raises(ValueError):
        build_fetch_command("bash", HTTPS_URL, DEST_POSIX, "linux", tls="verify")


def test_build_fetch_command_unknown_tool_raises():
    with pytest.raises(ValueError):
        build_fetch_command("magic", HTTP_URL, DEST_POSIX, "linux", tls="plaintext")
