"""A fake Proxmox environment for testing the deploy scripts.

`FakePve` builds a sandbox with a stand-in `pct` (tests/fakes/pct) first on
PATH and a state directory of fake containers, and runs bash snippets in it.
"""

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"

_GIT_BASH = Path(r"C:\Program Files\Git\usr\bin\bash.exe")
BASH = str(_GIT_BASH) if _GIT_BASH.is_file() else "bash"

DEFAULT_ENV = """# portal settings
PVE_HOST=titan.example.net
PVE_STORAGE=pbs-a
PORT=8008
DEFAULT_THEME=auto
RESTORE_DATA_NICS=[{"cidr": "192.168.1.0/24", "local_ip": "192.168.1.50"}]
"""


class FakePve:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / "pve"
        self.bin = self.root / "bin"
        (self.state / "ct").mkdir(parents=True)
        self.bin.mkdir()
        pct = self.bin / "pct"
        shutil.copy(ROOT / "tests" / "fakes" / "pct", pct)
        pct.chmod(pct.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        wt = self.bin / "whiptail"
        shutil.copy(ROOT / "tests" / "fakes" / "whiptail", wt)
        wt.chmod(wt.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        curl = self.bin / "curl"
        shutil.copy(ROOT / "tests" / "fakes" / "curl", curl)
        curl.chmod(curl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if shutil.which("python3") is None:  # e.g. Git Bash on Windows
            shim = self.bin / "python3"
            shim.write_text('#!/usr/bin/env bash\nexec python "$@"\n', encoding="utf-8", newline="\n")
            shim.chmod(0o755)

    def close(self) -> None:
        self._tmp.cleanup()

    def ct(self, ctid: int) -> Path:
        return self.state / "ct" / str(ctid)

    def add_ct(
        self,
        ctid: int,
        name: str = "portal",
        status: str = "running",
        tags: str = "",
        net0: str = "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:CC,ip=dhcp,type=veth",
        extra: str = "",
        env_text: str | None = DEFAULT_ENV,
        version: str | None = "1.9.0",
        addr: str | None = "192.168.1.50/24",
    ) -> Path:
        c = self.ct(ctid)
        (c / "fs" / "opt" / "pve-flr-portal").mkdir(parents=True)
        config = f"hostname: {name}\nnet0: {net0}\n"
        if tags:
            config += f"tags: {tags}\n"
        config += extra
        (c / "config").write_text(config, encoding="utf-8", newline="\n")
        (c / "status").write_text(status + "\n", encoding="utf-8", newline="\n")
        if env_text is not None:
            (c / "fs" / "opt" / "pve-flr-portal" / ".env").write_text(env_text, encoding="utf-8", newline="\n")
        if version is not None:
            (c / "fs" / "opt" / "pve-flr-portal" / "VERSION").write_text(version + "\n", encoding="utf-8", newline="\n")
        if addr is not None:
            (c / "addr").write_text(addr + "\n", encoding="utf-8", newline="\n")
        return c

    def install_tools(self, ctid: int) -> None:
        """Put the management tools in the container's checkout, as a release has them."""
        deploy = self.ct(ctid) / "fs" / "opt" / "pve-flr-portal" / "deploy"
        (deploy / "lib").mkdir(parents=True, exist_ok=True)
        shutil.copy(DEPLOY / "pfr-manage.sh", deploy / "pfr-manage.sh")
        shutil.copy(DEPLOY / "lib" / "settings.sh", deploy / "lib" / "settings.sh")

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": os.environ.get("HOME", str(self.root)),
            "FAKE_PVE": self.state.as_posix(),
            "PFR_WAIT_TRIES": "2",
            "PFR_WAIT_SLEEP": "0",
        }
        if os.name == "nt":
            # Without these the Windows Python launcher can't find its install
            # and unpacks a copy into the working directory.
            for key in ("SYSTEMROOT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP", "PYTHONHOME"):
                if key in os.environ:
                    env[key] = os.environ[key]
        env.update(extra or {})
        return env

    def bash(
        self, script: str, extra_env: dict[str, str] | None = None, stdin: str | None = None
    ) -> subprocess.CompletedProcess:
        path = self.root / "snippet.sh"
        path.write_text(script, encoding="utf-8", newline="\n")
        return self.run_script(path, [], extra_env, stdin)

    def run_script(
        self,
        script: Path,
        args: list[str],
        extra_env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess:
        # Input goes through a file so line endings stay LF on Windows too.
        stdin_file = None
        if stdin is not None:
            path = self.root / "stdin.txt"
            path.write_text(stdin, encoding="utf-8", newline="\n")
            stdin_file = path.open("rb")
        try:
            return subprocess.run(
                [BASH, script.as_posix(), *args],
                stdin=stdin_file if stdin_file is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
                env=self.env(extra_env),
            )
        finally:
            if stdin_file is not None:
                stdin_file.close()

    def read(self, ctid: int, rel: str) -> str:
        return (self.ct(ctid) / "fs" / rel.lstrip("/")).read_text(encoding="utf-8")

    def config(self, ctid: int) -> str:
        return (self.ct(ctid) / "config").read_text(encoding="utf-8")

    def log(self, name: str) -> str:
        p = self.state / name
        return p.read_text(encoding="utf-8") if p.exists() else ""
