"""Checks on deploy/host/patch-pve-host-backups.sh.

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
SCRIPT = ROOT / "deploy" / "host" / "patch-pve-host-backups.sh"

_GIT_BASH = Path(r"C:\Program Files\Git\usr\bin\bash.exe")
BASH = str(_GIT_BASH) if _GIT_BASH.is_file() else "bash"

OLD_LINE_1 = "next if !($btype eq 'vm' || $btype eq 'ct');"
# The pre-sentinel form a previous script version left behind live -
# must still be recognized as "already applied" (and upgraded), not a
# mismatch, by every version from here on.
LEGACY_NEW_LINE_1 = "next if !($btype eq 'vm' || $btype eq 'ct' || $btype eq 'host');"
NEW_LINE_1 = (
    "next if !($btype eq 'vm' || $btype eq 'ct' || $btype eq 'host'); "
    "# patched by pve-flr-portal, see deploy/host/patch-pve-host-backups.sh"
)
OLD_LINE_2 = "next if $bid !~ m/^\\d+$/;"
NEW_LINE_2 = "next if $btype ne 'host' && $bid !~ m/^\\d+$/;"
OLD_LINE_3 = "vmid => int($bid),"
NEW_LINE_3 = "vmid => ($btype eq 'host' ? undef : int($bid)),"
OLD_LINE_4 = "subtype => $btype eq 'vm' ? 'qemu' : 'lxc',"
NEW_LINE_4 = "subtype => $btype eq 'vm' ? 'qemu' : $btype eq 'ct' ? 'lxc' : undef,"
ALL_OLD = [OLD_LINE_1, OLD_LINE_2, OLD_LINE_3, OLD_LINE_4]
ALL_NEW = [NEW_LINE_1, NEW_LINE_2, NEW_LINE_3, NEW_LINE_4]


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


def _fixture_text(*lines: str) -> str:
    body = "\n    ".join(lines)
    return f"sub list_volumes {{\n    {body}\n}}\n"


def test_patch_py_apply_then_idempotent_reapply(tmp_path):
    fixture = tmp_path / "PBSPlugin.pm"
    fixture.write_text(_fixture_text(*ALL_OLD), encoding="utf-8")

    result = _run_patch_py(tmp_path, "apply")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHANGED"
    text = fixture.read_text(encoding="utf-8")
    for new_line in ALL_NEW:
        assert new_line in text

    result = _run_patch_py(tmp_path, "apply")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "NOOP"


def test_patch_py_remove_reverts_exactly(tmp_path):
    fixture = tmp_path / "PBSPlugin.pm"
    fixture.write_text(_fixture_text(*ALL_NEW), encoding="utf-8")

    result = _run_patch_py(tmp_path, "remove")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHANGED"
    text = fixture.read_text(encoding="utf-8")
    for old_line, new_line in zip(ALL_OLD, ALL_NEW):
        assert old_line in text
        assert new_line not in text


def test_patch_py_remove_on_never_patched_is_a_noop(tmp_path):
    fixture = tmp_path / "PBSPlugin.pm"
    fixture.write_text(_fixture_text(*ALL_OLD), encoding="utf-8")

    result = _run_patch_py(tmp_path, "remove")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "NOOP"


def test_patch_py_refuses_rather_than_guesses_on_a_changed_line(tmp_path):
    # Simulates a future PVE version rewording a target line.
    fixture = tmp_path / "PBSPlugin.pm"
    broken = _fixture_text("next if 0;", "next if 0;", "next if 0;", "next if 0;")
    fixture.write_text(broken, encoding="utf-8")

    result = _run_patch_py(tmp_path, "apply")
    assert result.returncode == 1
    assert "MISMATCH" in result.stderr
    assert fixture.read_text(encoding="utf-8") == broken


def test_patch_py_resumes_when_only_some_lines_were_patched_before(tmp_path):
    # Real scenario hit live: an earlier version of this script
    # only patched the first two lines. Re-running the current version
    # must finish the remaining two, not treat "some lines already new,
    # some still old" as an error - that's the normal resume case, not
    # a mismatch.
    fixture = tmp_path / "PBSPlugin.pm"
    fixture.write_text(_fixture_text(NEW_LINE_1, NEW_LINE_2, OLD_LINE_3, OLD_LINE_4), encoding="utf-8")

    result = _run_patch_py(tmp_path, "apply")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHANGED"
    text = fixture.read_text(encoding="utf-8")
    for new_line in ALL_NEW:
        assert new_line in text


def test_patch_py_upgrades_a_fully_patched_pre_sentinel_install(tmp_path):
    # Hit live: all four lines already patched by the version of
    # this script from before the sentinel comment existed - must add
    # the sentinel (an upgrade), not refuse the whole file as a mismatch
    # just because line 1's exact text changed between script versions.
    fixture = tmp_path / "PBSPlugin.pm"
    fixture.write_text(_fixture_text(LEGACY_NEW_LINE_1, NEW_LINE_2, NEW_LINE_3, NEW_LINE_4), encoding="utf-8")

    result = _run_patch_py(tmp_path, "apply")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHANGED"
    # LEGACY_NEW_LINE_1 is a literal substring of NEW_LINE_1 (same text,
    # minus the trailing comment) - asserting its absence would be
    # asserting a contradiction once NEW_LINE_1 is present, so this only
    # checks for the upgraded (comment-bearing) form.
    text = fixture.read_text(encoding="utf-8")
    assert NEW_LINE_1 in text

    # And now a second run is a true no-op.
    result = _run_patch_py(tmp_path, "apply")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "NOOP"


def test_patch_py_remove_also_recognizes_the_legacy_pre_sentinel_form(tmp_path):
    fixture = tmp_path / "PBSPlugin.pm"
    fixture.write_text(_fixture_text(LEGACY_NEW_LINE_1, NEW_LINE_2, NEW_LINE_3, NEW_LINE_4), encoding="utf-8")

    result = _run_patch_py(tmp_path, "remove")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHANGED"
    text = fixture.read_text(encoding="utf-8")
    assert OLD_LINE_1 in text
    assert LEGACY_NEW_LINE_1 not in text
    assert NEW_LINE_1 not in text


def test_patch_py_refuses_whole_batch_if_one_line_is_genuinely_unrecognized(tmp_path):
    # One line matches neither its expected old nor new form (e.g. PVE
    # reworded it) while the others are still perfectly normal - must
    # still refuse the whole file, not patch just the recognizable lines.
    fixture = tmp_path / "PBSPlugin.pm"
    broken = _fixture_text("next if 0;", OLD_LINE_2, OLD_LINE_3, OLD_LINE_4)
    fixture.write_text(broken, encoding="utf-8")

    result = _run_patch_py(tmp_path, "apply")
    assert result.returncode == 1
    assert "MISMATCH" in result.stderr
    assert fixture.read_text(encoding="utf-8") == broken


def test_script_requires_root():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'\[\s*"\$\(id -u\)"\s*-ne\s*0\s*\]', text)


def test_script_installs_a_dpkg_hook_that_reapplies_itself():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "DPkg::Post-Invoke" in text
    assert "/etc/apt/apt.conf.d/" in text
