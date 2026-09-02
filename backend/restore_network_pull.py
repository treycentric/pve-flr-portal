"""Design C (docs/plan.md §7.6, issue #22): the network-pull restore
mechanism - not yet wired into a live restore (that's a later step; see
the issue's sequencing). This module holds the two pieces of pure,
fully-testable-without-a-live-guest logic the design depends on:

- **Data-NIC selection.** With several mutually non-routable subnets, a
  bootstrap script's download URL only works if it points at the one
  data-NIC IP actually reachable from the target guest's subnet - so the
  app has to pick the right one per job, not just have one configured.
  `parse_data_nics()` reads the admin's subnet->local-IP config
  (`RESTORE_DATA_NICS`, JSON); `select_data_nic()` matches a guest's own
  reported IP(s) (from QGA's `agent/network-get-interfaces`, fetched
  elsewhere - this module doesn't call it) against those subnets.

- **Fetch-tool detection.** "Living off the land" (assuming
  curl/Invoke-WebRequest is present) is exactly the kind of assumption
  this project has been burned by before (certutil's output shape,
  copy /b's exit code, wmic's slowness) - so this probes for a fetch
  tool via cheap guest-exec checks rather than assuming one, walking a
  priority list per guest OS family and returning the first that's
  actually present. `None` means "nothing usable" - callers should
  treat that as Design C simply not being offered for this job, the
  same silent fallback to Design B that already happens when
  `VM.GuestAgent.Unrestricted` isn't granted.
"""
import ipaddress
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

ExecFn = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


@dataclass(frozen=True)
class DataNic:
    cidr: str
    local_ip: str

    def __post_init__(self) -> None:
        # Validated eagerly so a typo in the admin's config surfaces
        # clearly at the point it's parsed, not as a confusing failure
        # deep inside subnet-matching later.
        ipaddress.ip_network(self.cidr, strict=False)
        ipaddress.ip_address(self.local_ip)


class InvalidDataNicConfig(ValueError):
    pass


def parse_data_nics(raw: str) -> list[DataNic]:
    """Parses RESTORE_DATA_NICS - a JSON array of {"cidr": ..., "local_ip":
    ...} objects, one per non-routable subnet a target guest might live
    in. Empty/blank input means Design C is unconfigured (not an error -
    the feature is opt-in); a non-empty value that fails to parse is a
    real admin mistake and raises, rather than silently disabling the
    feature the admin thought they'd just turned on."""
    raw = (raw or "").strip()
    if not raw or raw == "[]":
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidDataNicConfig(f"RESTORE_DATA_NICS is not valid JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise InvalidDataNicConfig("RESTORE_DATA_NICS must be a JSON array")
    try:
        return [DataNic(cidr=e["cidr"], local_ip=e["local_ip"]) for e in entries]
    except (KeyError, TypeError) as exc:
        raise InvalidDataNicConfig(f"Each RESTORE_DATA_NICS entry needs 'cidr' and 'local_ip': {exc}") from exc
    except ValueError as exc:  # from DataNic.__post_init__'s ipaddress parsing
        raise InvalidDataNicConfig(str(exc)) from exc


def select_data_nic(guest_ips: list[str], data_nics: list[DataNic]) -> DataNic | None:
    """Picks the one configured data NIC whose subnet actually contains
    one of the guest's own reported IPs. Never guesses across subnets -
    no match means Design C isn't offered for this job (falls back to
    Design B), same capability-detection spirit as the rest of PH.5.
    Malformed guest-reported addresses are skipped, not fatal - QGA's
    reported interface list can include things like link-local/loopback
    entries this app doesn't need to understand."""
    for nic in data_nics:
        network = ipaddress.ip_network(nic.cidr, strict=False)
        for ip_str in guest_ips:
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if ip in network:
                return nic
    return None


# Priority-ordered per guest-OS-family candidates. Each tuple is
# (tool name, a cheap guest-exec argv that succeeds - exit 0 - only if
# the tool is actually usable). Order matters: the first one that's
# present wins, most-capable/most-common first.
_WINDOWS_CANDIDATES: list[tuple[str, list[str]]] = [
    ("Invoke-WebRequest", ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Command Invoke-WebRequest"]),
    ("certutil", ["where", "certutil.exe"]),
    ("bitsadmin", ["where", "bitsadmin.exe"]),
    ("cscript", ["where", "cscript.exe"]),
]

_POSIX_CANDIDATES: list[tuple[str, list[str]]] = [
    ("curl", ["sh", "-c", "command -v curl"]),
    ("wget", ["sh", "-c", "command -v wget"]),
    ("python3", ["sh", "-c", "command -v python3"]),
    ("python", ["sh", "-c", "command -v python"]),
    ("bash", ["sh", "-c", "command -v bash"]),  # last resort: hand-rolled /dev/tcp fetch
]


def _candidates_for(guest_os_family: str | None) -> list[tuple[str, list[str]]]:
    if guest_os_family == "windows":
        return _WINDOWS_CANDIDATES
    if guest_os_family in ("linux", "bsd", "macos"):
        return _POSIX_CANDIDATES
    return []  # unknown OS family - nothing to safely probe


@dataclass(frozen=True)
class FetchPlan:
    """What it takes to actually run one fetch-tool's command in the
    guest. `stage_content`/`stage_path` are set only for tools that need
    a script staged via `agent/file-write` first (currently just
    `cscript`, which needs a real .vbs file - `agent/exec` has no stdin
    piping to hand it a script inline); every other tool's fetch is a
    single guest-exec call, `exec_argv` alone."""

    exec_argv: list[str]
    stage_content: str | None = None
    stage_path: str | None = None


def build_fetch_command(
    tool: str, url: str, destination: str, guest_os_family: str | None, stage_path: str | None = None
) -> FetchPlan:
    """Builds the guest-exec command for one detected fetch tool. `url`
    and `destination` are embedded directly in shell/PowerShell-
    interpreted command strings for several of these tools - the same
    trust assumption `restore_runner.py`'s `_concat_chunks`/
    `_restore_mtime` already make: `destination` must already have
    passed `pve_client.check_path_safe()` (done once, up front, by the
    caller before any of this runs), and `url` is never user input - it's
    built entirely from this app's own validated pieces (a configured
    data-NIC IP, this app's own port, a random token), never anything a
    guest or a user supplies.

    `stage_path` is required for `cscript` (see FetchPlan's docstring)
    and ignored for every other tool - the caller picks the actual path
    (typically via `restore_chunking.scratch_dir_path`/a job-scoped
    filename) since only it knows the job's scratch directory.

    **Unverified live** (docs/plan.md §7.6): none of these have been
    tested against a real guest yet - same caution this project already
    applies elsewhere (certutil's hash-output shape, `copy /b`'s exit
    code) before trusting a Windows/POSIX command's exact behavior.
    """
    if tool == "Invoke-WebRequest":
        script = f"Invoke-WebRequest -Uri '{url}' -OutFile '{destination}'"
        return FetchPlan(exec_argv=["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    if tool == "certutil":
        return FetchPlan(exec_argv=["certutil", "-urlcache", "-split", "-f", url, destination])
    if tool == "bitsadmin":
        # Plain argv, not `cmd /c 'bitsadmin ... "{url}" "{destination}"'`
        # - the same `cmd /c` "multiple embedded double-quoted segments
        # on one /c line" pattern already confirmed live (2026-09-01,
        # 2026-09-02) to be unreliable elsewhere in this project
        # (_ensure_destination_dir(), _concat_chunks()) even against
        # perfectly valid paths. bitsadmin.exe is a real executable, so
        # it can be invoked directly - each argv element reaches it
        # literally, no cmd.exe quote-parsing involved at all - matching
        # how `certutil` above is already called.
        return FetchPlan(
            exec_argv=["bitsadmin", "/transfer", "pve-flr-portal-restore", "/priority", "normal", url, destination]
        )
    if tool == "cscript":
        if not stage_path:
            raise ValueError("cscript needs a stage_path to write its .vbs script to")
        vbs = (
            'Dim http, stream\n'
            'Set http = CreateObject("WinHttp.WinHttpRequest.5.1")\n'
            f'http.Open "GET", "{url}", False\n'
            "http.Send\n"
            'Set stream = CreateObject("ADODB.Stream")\n'
            "stream.Type = 1\n"  # binary
            "stream.Open\n"
            "stream.Write http.ResponseBody\n"
            f'stream.SaveToFile "{destination}", 2\n'  # 2 = overwrite
            "stream.Close\n"
        )
        return FetchPlan(
            exec_argv=["cscript", "//nologo", "//B", stage_path],
            stage_content=vbs,
            stage_path=stage_path,
        )
    if tool == "curl":
        return FetchPlan(exec_argv=["curl", "-fsSL", "-o", destination, url])
    if tool == "wget":
        return FetchPlan(exec_argv=["wget", "-q", "-O", destination, url])
    if tool in ("python3", "python"):
        py = f"import urllib.request; urllib.request.urlretrieve({url!r}, {destination!r})"
        return FetchPlan(exec_argv=[tool, "-c", py])
    if tool == "bash":
        # Last resort: a hand-rolled HTTP/1.0 GET over bash's /dev/tcp
        # pseudo-device, no external binary at all. Parses the URL's
        # host/port/path in Python (this app's own validated pieces, per
        # this function's docstring - never guest input) so the guest
        # only ever has to run a fixed-shape script, then strips the
        # HTTP response headers (everything up to the first blank line)
        # before writing the rest to the destination.
        #
        # Real limitation, not an oversight: /dev/tcp is a plain TCP
        # socket - bash has no built-in TLS, so this cannot speak HTTPS.
        # Every other tool on this list (even certutil/bitsadmin) handles
        # TLS natively; this is the one candidate that can't, which is
        # exactly why it's last in the priority list. Rather than
        # generate a script that will fail confusingly against an https
        # URL, this raises clearly so the caller finds out now.
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme != "http":
            raise ValueError(
                "The bash /dev/tcp fetch fallback cannot speak TLS - it only works against a plain "
                f"http:// download URL, got {url!r}. If this is the only available fetch tool for this "
                "guest, Design C isn't usable here unless the data-NIC download route is served over "
                "plain HTTP (acceptable given it's already firewalled/token-gated, per docs/plan.md §7.6)."
            )
        host = parts.hostname
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        script = (
            f"exec 3<>/dev/tcp/{host}/{port}\n"
            f'printf "GET %s HTTP/1.0\\r\\nHost: %s\\r\\nConnection: close\\r\\n\\r\\n" "{path}" "{host}" >&3\n'
            "awk 'f{print} /^\\r$/{f=1}' <&3 > "
            f"'{destination}'\n"
        )
        return FetchPlan(exec_argv=["bash", "-c", script])
    raise ValueError(f"Unknown fetch tool: {tool!r}")


async def detect_fetch_tool(exec_fn: ExecFn, guest_os_family: str | None) -> str | None:
    """Walks the priority list for this guest's OS family, running one
    cheap guest-exec probe per candidate, and returns the name of the
    first one that's actually present. `exec_fn` is injected (rather
    than this module calling pve_client directly) so it's testable with
    a fake, the same pattern restore_runner.py's own `_exec` wrapper
    exists for. Returns None if nothing on the list is available (or
    the OS family is unknown) - callers should treat that as "Design C
    isn't offered for this job", never a hard failure."""
    for tool_name, probe_argv in _candidates_for(guest_os_family):
        try:
            exitcode, _out, _err = await exec_fn(probe_argv)
        except Exception:
            continue  # a probe itself failing (timeout, etc.) just means "try the next one"
        if exitcode == 0:
            return tool_name
    return None
