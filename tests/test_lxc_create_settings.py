"""deploy/lxc-create.sh's setting resolution, exercised for real.

Needs a PVE host to actually create a container, so these run only the
part of the script that resolves settings (everything above "Container
creation") in a bash subprocess with no terminal attached - the unattended
path - and read the resulting variables back. The interactive whiptail
screens can't be driven from here.
"""

import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "lxc-create.sh"

_GIT_BASH = Path(r"C:\Program Files\Git\usr\bin\bash.exe")
BASH = str(_GIT_BASH) if _GIT_BASH.is_file() else "bash"

_MARKER = "# Container creation, consuming the resolved variables above."
_VARS = (
    "ENABLE_HOST_BACKUPS",
    "ENABLE_DNT",
    "IPV6_MODE",
    "IP_CONFIG",
    "PVE_STORAGE",
    "RESTORE_DATA_NICS_JSON",
    "PORTAL_PORT",
)


def _settings_section() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    head = text[: text.index(_MARKER)]
    return head.rsplit("# ----", 1)[0]


def _run(env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    echoes = "\n".join(f'echo "{v}=${{{v}:-}}"' for v in _VARS)
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    env.update(env_extra or {})
    # Via a file, not `bash -c`: the section is too long for a Windows
    # command line.
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "settings.sh"
        script.write_text(_settings_section() + "\n" + echoes + "\n", encoding="utf-8", newline="\n")
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
