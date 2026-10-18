"""How deploy/lxc-create.sh gets its shared library (deploy/lib/settings.sh)
when it is curl-piped rather than run from a checkout: the latest release
first, then main if that release predates the library or has a different
API. Uses a fake curl (tests/fakes/curl)."""

import re

import pytest
from pve_fakes import DEPLOY, FakePve

SCRIPT = (DEPLOY / "lxc-create.sh").read_text(encoding="utf-8")
LOAD_LIB = re.search(r"^load_lib\(\) \{\n.*?^\}\n", SCRIPT, re.S | re.M).group(0)


@pytest.fixture
def pve():
    fake = FakePve()
    yield fake
    fake.close()


def _lib(pve: FakePve, name: str, api: int, marker: str) -> str:
    path = pve.root / name
    path.write_text(f"PFR_LIB_API={api}\nPFR_FROM={marker}\n", encoding="utf-8", newline="\n")
    return path.as_posix()


def fetch(pve: FakePve, **env: str):
    script = (
        "set -euo pipefail\n"
        'msg_err() { echo "ERR: $*" >&2; }\n'
        'REPO_URL="https://github.com/octo/portal.git"\n'
        "PFR_LIB_API_WANTED=1\n" + LOAD_LIB + "load_lib\n"
        'echo "from=$PFR_FROM"\n'
    )
    return pve.bash(script, extra_env=env)


def test_uses_the_latest_release(pve):
    result = fetch(
        pve,
        FAKE_LATEST_URL="https://github.com/octo/portal/releases/tag/v1.9.0",
        FAKE_TAG_LIB=_lib(pve, "tag.sh", 1, "tag"),
        FAKE_MAIN_LIB=_lib(pve, "main.sh", 1, "main"),
    )
    assert result.returncode == 0, result.stderr
    assert "from=tag" in result.stdout


def test_falls_back_to_main_when_the_release_predates_the_library(pve):
    result = fetch(
        pve,
        FAKE_LATEST_URL="https://github.com/octo/portal/releases/tag/v1.9.0",
        FAKE_MAIN_LIB=_lib(pve, "main.sh", 1, "main"),  # no tag lib: curl fails for it
    )
    assert result.returncode == 0, result.stderr
    assert "from=main" in result.stdout


def test_falls_back_to_main_when_the_release_has_a_different_api(pve):
    result = fetch(
        pve,
        FAKE_LATEST_URL="https://github.com/octo/portal/releases/tag/v1.9.0",
        FAKE_TAG_LIB=_lib(pve, "tag.sh", 2, "tag"),
        FAKE_MAIN_LIB=_lib(pve, "main.sh", 1, "main"),
    )
    assert result.returncode == 0, result.stderr
    assert "from=main" in result.stdout


def test_uses_main_when_there_is_no_release_yet(pve):
    result = fetch(
        pve,
        FAKE_LATEST_URL="https://github.com/octo/portal/releases",
        FAKE_MAIN_LIB=_lib(pve, "main.sh", 1, "main"),
    )
    assert result.returncode == 0, result.stderr
    assert "from=main" in result.stdout


def test_a_clear_error_when_no_library_can_be_fetched(pve):
    result = fetch(pve, FAKE_LATEST_URL="https://github.com/octo/portal/releases/tag/v1.9.0")
    assert result.returncode == 1
    assert "shared library" in result.stderr


def test_uses_the_library_next_to_the_script_in_a_checkout(pve, tmp_path):
    checkout = tmp_path / "deploy"
    (checkout / "lib").mkdir(parents=True)
    (checkout / "lib" / "settings.sh").write_text("PFR_LIB_API=1\nPFR_FROM=checkout\n", encoding="utf-8", newline="\n")
    script = checkout / "run.sh"
    script.write_text(
        "set -euo pipefail\n"
        'msg_err() { echo "ERR: $*" >&2; }\n'
        'REPO_URL="https://github.com/octo/portal.git"\n'
        "PFR_LIB_API_WANTED=1\n" + LOAD_LIB + 'load_lib\necho "from=$PFR_FROM"\n',
        encoding="utf-8",
        newline="\n",
    )
    result = pve.run_script(script, [])
    assert result.returncode == 0, result.stderr
    assert "from=checkout" in result.stdout
