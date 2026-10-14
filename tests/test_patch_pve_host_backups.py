"""Checks on deploy/patch-pve-host-backups.sh (issue #155).

Can't exercise the real thing end-to-end - it needs root and a real PVE
host's PBSPlugin.pm. What's checked here: the script is valid bash, and
the embedded Python patch logic (extracted from its own heredoc, not
reimplemented, so this can't silently drift from what actually runs)
does the right thing against a fixture file standing in for
PBSPlugin.pm: apply, idempotent re-apply, remove, and refusing rather
than guessing when the target line doesn't match.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "patch-pve-host-backups.sh"

_GIT_BASH = Path(r"C:\Program Files\Git\usr\bin\bash.exe")
BASH = str(_GIT_BASH) if _GIT_BASH.is_file() else "bash"

OLD_LINE = "next if !($btype eq 'vm' || $btype eq 'ct');"
NEW_LINE = "next if !($btype eq 'vm' || $btype eq 'ct' || $btype eq 'host');"


def _extract_patch_py() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", text, re.DOTALL)
    assert match, "couldn't find the _patch_py heredoc in patch-pve-host-backups.sh"
    return match.group(1)


def _run_patch_py(tmp_path, mode: str) -> subprocess.CompletedProcess:
    script_py = tmp_path / "_patch.py"
    script_py.write_text(_extract_patch_py(), encoding="utf-8")
    return subprocess.run(
        ["python3" if _which("python3") else "python", str(script_py), str(tmp_path / "PBSPlugin.pm"), mode],
        capture_output=True,
        text=True,
    )


def _which(cmd: str) -> bool:
    from shutil import which

    return which(cmd) is not None


def test_script_is_valid_bash():
    result = subprocess.run([BASH, "-n", SCRIPT.as_posix()], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_patch_py_apply_then_idempotent_reapply(tmp_path):
    fixture = tmp_path / "PBSPlugin.pm"
    fixture.write_text(f"sub list_volumes {{\n    {OLD_LINE}\n}}\n", encoding="utf-8")

    result = _run_patch_py(tmp_path, "apply")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHANGED"
    assert NEW_LINE in fixture.read_text(encoding="utf-8")

    result = _run_patch_py(tmp_path, "apply")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "NOOP"


def test_patch_py_remove_reverts_exactly(tmp_path):
    fixture = tmp_path / "PBSPlugin.pm"
    fixture.write_text(f"sub list_volumes {{\n    {NEW_LINE}\n}}\n", encoding="utf-8")

    result = _run_patch_py(tmp_path, "remove")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHANGED"
    text = fixture.read_text(encoding="utf-8")
    assert OLD_LINE in text
    assert NEW_LINE not in text


def test_patch_py_remove_on_never_patched_is_a_noop(tmp_path):
    fixture = tmp_path / "PBSPlugin.pm"
    fixture.write_text(f"sub list_volumes {{\n    {OLD_LINE}\n}}\n", encoding="utf-8")

    result = _run_patch_py(tmp_path, "remove")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "NOOP"


def test_patch_py_refuses_rather_than_guesses_on_a_changed_line(tmp_path):
    # Simulates a future PVE version rewording the target line.
    fixture = tmp_path / "PBSPlugin.pm"
    fixture.write_text("sub list_volumes {\n    next if 0;\n}\n", encoding="utf-8")

    result = _run_patch_py(tmp_path, "apply")
    assert result.returncode == 1
    assert "MISMATCH" in result.stderr
    assert fixture.read_text(encoding="utf-8") == "sub list_volumes {\n    next if 0;\n}\n"


def test_script_requires_root():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'\[\s*"\$\(id -u\)"\s*-ne\s*0\s*\]', text)


def test_script_installs_a_dpkg_hook_that_reapplies_itself():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "DPkg::Post-Invoke" in text
    assert "/etc/apt/apt.conf.d/" in text


def test_lxc_create_defaults_to_declining_when_noninteractive():
    text = (ROOT / "deploy" / "lxc-create.sh").read_text(encoding="utf-8")
    # The non-interactive branch (no tty) must default to false, never
    # hang on `read` or silently patch anything.
    assert re.search(r'if \[ -t 0 \]', text)
    assert re.search(r"else\s*\n\s*ENABLE_HOST_BACKUPS=false", text)
