"""Static checks on the deploy/ shell scripts and systemd unit template.

A compromised service process (runs as the unprivileged
APP_USER) must not be able to write anywhere under the app dir except
certs/ - a broad `chown -R APP_USER:APP_USER "$APP_DIR"` (or an
equally broad `ReadWritePaths=` in the unit) would let it modify code,
.venv, or .git hooks that root later runs on the next update, a
service-to-root escalation path. These scripts can't be exercised
end-to-end without a real root shell/systemd, so this only checks the
source text for the patterns that matter.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"

BROAD_CHOWN = re.compile(r'chown\s+-R\s+"?\$APP_USER"?:"?\$APP_USER"?\s+"?\$APP_DIR"?\s*$', re.MULTILINE)

# On Windows, plain "bash" can resolve to the WSL launcher shim in
# System32 instead of Git Bash - it doesn't understand a Windows-style
# path at all ("No such file or directory" even with forward slashes).
# Prefer Git Bash explicitly when present; CI (ubuntu-latest) has no
# such ambiguity, so the plain fallback is fine there.
_GIT_BASH = Path(r"C:\Program Files\Git\usr\bin\bash.exe")
BASH = str(_GIT_BASH) if _GIT_BASH.is_file() else "bash"


def _path(name: str) -> Path:
    """A deploy script by its file name: host/ scripts run on the node, container/ ones inside."""
    for sub in ("container", "host"):
        if (DEPLOY / sub / name).exists():
            return DEPLOY / sub / name
    raise AssertionError(f"no deploy script named {name}")


def _script(name: str) -> str:
    return _path(name).read_text(encoding="utf-8")


def test_deploy_scripts_are_valid_bash():
    for name in ("install.sh", "update.sh", "lxc-create.sh"):
        # .as_posix() - Git Bash parses forward slashes fine but mangles
        # a raw Windows backslash path, since backslash is bash's own
        # escape character.
        result = subprocess.run([BASH, "-n", _path(name).as_posix()], capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_install_and_update_never_broadly_chown_the_app_dir_to_the_service_user():
    for name in ("install.sh", "update.sh"):
        text = _script(name)
        assert not BROAD_CHOWN.search(text), f"{name} still chowns the whole app dir to APP_USER"


def test_install_and_update_root_own_the_app_dir_then_carve_out_certs():
    for name in ("install.sh", "update.sh"):
        text = _script(name)
        assert re.search(r'chown\s+-R\s+root:root\s+"?\$APP_DIR"?', text), f"{name}: missing root:root chown"
        assert re.search(r'chown\s+-R\s+"?\$APP_USER"?:"?\$APP_USER"?\s+"?\$APP_DIR"?/certs', text), (
            f"{name}: missing certs/ ownership for APP_USER"
        )


def test_install_and_update_lock_down_env_file_ownership_and_mode():
    for name in ("install.sh", "update.sh"):
        text = _script(name)
        assert re.search(r'chown\s+root:"?\$APP_USER"?\s+"?\$APP_DIR"?/\.env', text), f"{name}: .env not root:APP_USER"
        assert re.search(r'chmod\s+640\s+"?\$APP_DIR"?/\.env', text), f"{name}: .env not chmod 640"


def test_install_enables_then_restarts_rather_than_enable_now():
    text = _script("install.sh")
    # Line-anchored (not a bare substring check) - the rationale comment
    # explaining *why* this isn't used legitimately contains the phrase
    # "enable --now" as prose, which a substring check would misfire on.
    assert not re.search(r"^\s*systemctl\s+enable\s+--now", text, re.MULTILINE), (
        "install.sh still runs systemctl enable --now (won't restart an already-running unit)"
    )
    assert re.search(r"systemctl\s+enable\s+\"\$SERVICE_NAME\"", text)
    assert re.search(r"systemctl\s+restart\s+\"\$SERVICE_NAME\"", text)


def test_update_refreshes_the_systemd_unit_and_reloads_the_daemon():
    text = _script("update.sh")
    assert "pve-flr-portal.service.template" in text, "update.sh no longer regenerates the unit file"
    assert "systemctl daemon-reload" in text


def test_install_and_update_require_root():
    for name in ("install.sh", "update.sh"):
        text = _script(name)
        assert re.search(r'\[\s*"\$\(id -u\)"\s*-ne\s*0\s*\]', text), f"{name}: missing a root check"


def test_install_and_update_verify_ownership_and_mode_took_effect():
    for name in ("install.sh", "update.sh"):
        text = _script(name)
        for target, owner in (
            ("certs", "$APP_USER:$APP_USER"),
            (".env", "root:$APP_USER"),
        ):
            assert f'check_owner "$APP_DIR/{target}" "{owner}"' in text, f"{name}: missing ownership check for {target}"
        assert 'check_mode "$APP_DIR/certs" "750"' in text, f"{name}: missing mode check for certs/"
        assert 'check_mode "$APP_DIR/.env" "640"' in text, f"{name}: missing mode check for .env"


def test_unit_template_grants_write_access_to_certs_only():
    text = (DEPLOY / "container" / "pve-flr-portal.service.template").read_text(encoding="utf-8")
    match = re.search(r"^ReadWritePaths=(.+)$", text, re.MULTILINE)
    assert match is not None, "no ReadWritePaths= line in the unit template"
    assert match.group(1).strip() == "__APP_DIR__/certs"


def test_only_the_needed_os_packages_are_installed():
    # recommends of python3-pip are the whole compiler toolchain; the venv has its own pip
    install = _script("install.sh")
    installer = _script("lxc-create.sh")
    lines = [line for line in (install + installer).splitlines() if "apt-get install" in line]
    assert lines
    for line in lines:
        assert "--no-install-recommends" in line, line
    assert not re.search(r"python3-pip|build-essential|python3-dev|\bgcc\b", "\n".join(lines))
