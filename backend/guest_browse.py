"""PH.5: browse a guest's filesystem via `guest-exec`, so a restore
destination directory can be picked rather than typed blind
(docs/plan.md §7.5). There is no dedicated QGA directory-listing
command - only `guest-exec` running a platform listing tool - so this
needs `VM.GuestAgent.Unrestricted`, the same gate as metadata restore
and verify.
"""
import ntpath
import posixpath

from .auth import SessionData
from .pve_client import GuestExecTimeout, check_path_safe, run_guest_exec
from .pve_client import UnsafePathError as UnsafePathError  # re-exported, see below

# Re-exported for existing callers/tests - guest_browse.py used to own
# this path-safety check; it's shared with restore_runner.py now (both
# need to validate a path before it reaches a shell-interpreted
# guest-exec command), so the real implementation lives in pve_client.py.
_check_path_safe = check_path_safe


class ListingError(RuntimeError):
    pass


async def _run_exec(session: SessionData, guest_type: str, vmid: str, argv: list[str]) -> tuple[int, str, str]:
    """Thin wrapper around pve_client.run_guest_exec translating its
    timeout into this module's own ListingError, so callers below don't
    need their own try/except at every call site."""
    try:
        return await run_guest_exec(session, guest_type, vmid, argv)
    except GuestExecTimeout as exc:
        raise ListingError(str(exc)) from exc


def _windows_parent(path: str) -> str | None:
    normalized = path.rstrip("\\") or path
    parent_raw = ntpath.dirname(normalized)
    if not parent_raw or parent_raw.rstrip("\\").upper() == normalized.rstrip("\\").upper():
        return None  # already at a drive root - "up" means back to the drive list
    return parent_raw if parent_raw.endswith("\\") else parent_raw + "\\"


def _posix_parent(target: str) -> str | None:
    if target == "/":
        return None
    return posixpath.dirname(target.rstrip("/")) or "/"


# Legacy Windows compatibility junctions (pointing at C:\Users and its
# subfolders). The PowerShell listing above already filters these out of
# any directory it successfully lists (ReparsePoint attribute), so they
# won't normally show up as clickable entries at all - this is a
# defensive fallback for the case a user types one of these paths
# directly in manual-entry mode, where Windows still blocks listing into
# it (by design, even for SYSTEM) and `dir`/PowerShell alike report it as
# not found rather than listing it.
_KNOWN_WINDOWS_JUNCTIONS = {"documents and settings", "application data", "local settings", "my documents"}


def _friendlier_windows_listing_error(path: str, raw_message: str) -> str:
    leaf = path.rstrip("\\").rsplit("\\", 1)[-1].lower()
    if leaf in _KNOWN_WINDOWS_JUNCTIONS:
        return (
            f"'{path}' is a legacy Windows compatibility shortcut that can't be browsed directly "
            "(Windows blocks listing it, even for SYSTEM) - try C:\\Users instead."
        )
    return raw_message


async def list_directories(
    session: SessionData, guest_type: str, vmid: str, guest_os_family: str | None, path: str | None
) -> dict:
    """{"path", "parent", "separator", "entries": [{"name", "path"}, ...]}.
    `path` of None/"" means "show the top level" - drives on Windows,
    "/" on everything else."""
    is_windows = guest_os_family == "windows"
    sep = "\\" if is_windows else "/"

    if path:
        _check_path_safe(path)

    if is_windows:
        if not path:
            # PowerShell's Get-PSDrive, not `wmic` - `wmic` is legacy/
            # deprecated and goes through the WMI provider host (winmgmt),
            # which has real cold-start overhead, especially on first use
            # after boot; observed as noticeable sluggishness in practice.
            # Get-PSDrive is a native cmdlet with no WMI round-trip, and
            # keeps this consistent with the subfolder listing below,
            # which already switched to PowerShell.
            exitcode, out, err = await _run_exec(
                session,
                guest_type,
                vmid,
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-PSDrive -PSProvider FileSystem | Select-Object -ExpandProperty Root",
                ],
            )
            if exitcode != 0:
                raise ListingError(err.strip() or out.strip() or f"Listing failed (exit {exitcode})")
            # Each line is already a root like "C:\" - Get-PSDrive's .Root
            # includes the trailing separator.
            roots = [ln.strip() for ln in out.splitlines() if ln.strip()]
            entries = [{"name": r.rstrip("\\"), "path": r} for r in roots]
            return {"path": None, "parent": None, "separator": sep, "entries": entries}

        # PowerShell, not `dir`, because filtering out junctions/symlinked
        # directories needs the ReparsePoint attribute - `dir /b` (bare
        # names) can't distinguish a real directory from one, and even
        # `dir`'s non-bare <JUNCTION> marker would mean parsing its
        # locale-dependent date/time columns.
        #
        # Real-world finding: passing `path` as a trailing argv element
        # after -Command and referencing it as $args[0] does NOT work -
        # confirmed live ("Cannot bind argument to parameter 'LiteralPath'
        # because it is null"). With -Command, PowerShell appends trailing
        # CLI arguments onto the end of the *command string itself* rather
        # than binding them to $args inside the script, so $args[0] was
        # never actually populated. Embedding `path` as a single-quoted
        # PowerShell string literal instead is safe here specifically
        # because _check_path_safe() (called above, before this branch)
        # already rejects `'` along with the other shell-metacharacters -
        # a single-quoted PowerShell string doesn't interpret anything
        # else ($ variables, backticks, etc.), so there's nothing left in
        # an already-validated path that could break out of the literal.
        script = (
            f"try {{ Get-ChildItem -LiteralPath '{path}' -Directory -Force -ErrorAction Stop | "
            "Where-Object { -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } | "
            "Select-Object -ExpandProperty Name } catch { Write-Error $_; exit 1 }"
        )
        exitcode, out, err = await _run_exec(
            session, guest_type, vmid, ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        )
        if exitcode != 0:
            raw = err.strip() or out.strip() or f"Listing failed (exit {exitcode})"
            raise ListingError(_friendlier_windows_listing_error(path, raw))
        names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        base = path if path.endswith("\\") else path + "\\"
        entries = [{"name": n, "path": base + n} for n in names]
        return {"path": path, "parent": _windows_parent(path), "separator": sep, "entries": entries}

    target = path or "/"
    exitcode, out, err = await _run_exec(
        session, guest_type, vmid, ["find", target, "-mindepth", "1", "-maxdepth", "1", "-type", "d"]
    )
    if exitcode != 0:
        raise ListingError(err.strip() or out.strip() or f"Listing failed (exit {exitcode})")
    entries = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append({"name": posixpath.basename(line), "path": line})
    return {"path": target, "parent": _posix_parent(target), "separator": sep, "entries": entries}
