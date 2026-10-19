"""The installer's setting resolution (deploy/host/lib/settings.sh), exercised for real.

Creating a container needs a PVE host, so these source the shared library
in a bash subprocess with no terminal attached - the unattended path - run
the installer's settings steps, and read the resulting variables back. The
interactive whiptail screens can't be driven from here.
"""

import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "deploy" / "host" / "lib" / "settings.sh"

_GIT_BASH = Path(r"C:\Program Files\Git\usr\bin\bash.exe")
BASH = str(_GIT_BASH) if _GIT_BASH.is_file() else "bash"

_VARS = (
    "ENABLE_HOST_BACKUPS",
    "ENABLE_DNT",
    "IPV6_MODE",
    "IP_CONFIG",
    "PVE_STORAGE",
    "RESTORE_DATA_NICS_JSON",
    "PORTAL_PORT",
    "CREATE_ROLES",
)


def _run(env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    echoes = "\n".join(f'echo "{v}=${{{v}:-}}"' for v in _VARS)
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    env.update(env_extra or {})
    # Via a file rather than `bash -c`, to keep the command line short.
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "run.sh"
        script.write_text(
            "set -euo pipefail\n"
            f'source "{LIB.as_posix()}"\n'
            "pfr_detect_ui\npfr_init_install\npfr_run_install_ui\npfr_derive_install\n" + echoes + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return subprocess.run(
            [BASH, script.as_posix()],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )


def resolve_noninteractive(env_extra: dict[str, str] | None = None) -> dict[str, str]:
    result = _run(env_extra)
    assert result.returncode == 0, result.stderr
    out = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in _VARS:
            out[key] = value
    return out


def test_noninteractive_defaults_to_declining_host_backup_patching():
    # No tty: never hang on a prompt, never silently patch the PVE host.
    assert resolve_noninteractive()["ENABLE_HOST_BACKUPS"] == "false"


def test_noninteractive_defaults():
    got = resolve_noninteractive()
    assert got["ENABLE_DNT"] == "yes"
    assert got["IPV6_MODE"] == "auto"
    assert got["IP_CONFIG"] == "dhcp"
    assert got["PVE_STORAGE"] == "pbs"
    assert got["PORTAL_PORT"] == "8008"


def test_environment_variables_override_the_defaults():
    got = resolve_noninteractive(
        {
            "ENABLE_HOST_BACKUPS": "true",
            "ENABLE_DNT": "no",
            "IPV6_MODE": "disabled",
            "IP_CONFIG": "10.0.0.5/24,gw=10.0.0.1",
            "PVE_STORAGE": "pbs-a,pbs-b",
        }
    )
    assert got["ENABLE_HOST_BACKUPS"] == "true"
    assert got["ENABLE_DNT"] == "no"
    assert got["IPV6_MODE"] == "disabled"
    assert got["IP_CONFIG"] == "10.0.0.5/24,gw=10.0.0.1"
    assert got["PVE_STORAGE"] == "pbs-a,pbs-b"


def test_noninteractive_rejects_conflicting_settings():
    result = _run({"TLS_PREFERRED": "plaintext", "TLS_MINIMUM": "verify"})
    assert result.returncode != 0
    assert "stricter" in result.stderr

    result = _run({"IPV6_MODE": "static"})
    assert result.returncode != 0
    assert "IPv6" in result.stderr


def test_unattended_installs_create_no_roles_unless_asked():
    # roles are PVE-wide authorization config: nothing is created without CREATE_ROLES
    assert resolve_noninteractive()["CREATE_ROLES"] == ""
    got = resolve_noninteractive({"CREATE_ROLES": "FileRestoreReader,FileRestoreOperator"})
    assert got["CREATE_ROLES"] == "FileRestoreReader,FileRestoreOperator"


def test_an_unknown_role_in_the_environment_stops_an_unattended_install():
    result = _run({"CREATE_ROLES": "FileRestoreReader,Bogus"})
    assert result.returncode != 0
    assert "Unknown role 'Bogus'" in result.stderr
