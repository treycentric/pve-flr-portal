# Proxmox File Level Restore Portal (pve-flr-portal)

A small companion app for Proxmox VE + Proxmox Backup Server: a
scrubbable snapshot timeline and file browser for file-level restore,
in the spirit of Synology Active Backup for Business's restore portal.

Proxmox's own file-restore feature already does the hard part (safely
reading an arbitrary guest filesystem out of a backup via a throwaway
helper VM) — it's just missing a way to scrub across snapshots instead
of picking one at a time from a flat list. This project adds that on
top of Proxmox's existing API, without modifying Proxmox itself.

See [`docs/plan.md`](docs/plan.md) for the full plan: architecture,
scope decisions, UI mapping against the ABB reference, data model,
phased roadmap, and known risks.

**Status:** Phases 1-4 (browse, download, timeline, per-user auth) are
in place. See the roadmap in docs/plan.md; PH.5 (push-to-guest) is the
remaining stretch phase.

Auth is per-user PVE ticket login (docs/plan.md §7.1) — there's no
shared service token anymore. To grant a user access, see the
`pveum`/GUI steps in docs/plan.md §7.1 ("Admin steps to onboard a new
user").

## Running it

```
git clone https://github.com/treycentric/pve-flr-portal.git
cd pve-flr-portal

python -m venv .venv
source .venv/Scripts/activate   # .venv/bin/activate on Linux/macOS
pip install -r requirements.txt

cp .env.example .env
# edit .env: fill in PVE_HOST/PVE_STORAGE for your environment

python run.py
```

The app serves HTTPS by default on port **8008** (a self-signed cert is
generated automatically on first run at `certs/portal.crt`/`portal.key`
if you haven't dropped in your own). Open **https://127.0.0.1:8008/** —
your browser will warn about the self-signed cert the first time; that's
expected for a homelab self-signed setup. Drop a CA-issued cert/key at
the same paths to replace it.

Log in with your own PVE username/password (e.g. `admin@pam`). See
docs/plan.md §7.1 for how to grant a user the `FileRestoreReader` role
needed to browse/restore.

## Deployment

**LXC on your PVE host (recommended).** Run on the PVE host itself:

```
bash deploy/lxc-create.sh
```

Creates an unprivileged Debian 12 container, installs the app, and
starts it as a systemd service (`pve-flr-portal`). Override
`CTID`/`STORAGE`/`BRIDGE`/`MEMORY_MB`/etc. via environment variables -
see the top of the script. Already have a container? Run
`deploy/install.sh` inside it instead. Rationale for LXC over a Debian
package on the host or a full VM/OVA is in docs/plan.md §10.

**Docker, mainly for local dev/testing:**

```
docker compose up --build
```

Reads `.env` from the repo root and persists the generated cert under
`./certs`. This is how the download-format (.zip/.tar.gz/.tar.zst) and
login flows got exercised on both Python 3.14 (dev machine) and a
clean Python 3.11 (the deploy target) during development.

## Tests

```
./run-tests.ps1     # Windows (PowerShell)
./run-tests.sh      # Linux / macOS / Git Bash
```

Covers lint (`ruff`), Python (`pytest`), the `app.js` frontend logic
(`node --test`), and CSS (`stylelint`). See [`tests/README.md`](tests/README.md).
Runs in CI on every push/PR and again as a release gate — see
[`docs/dev/versioning.md`](docs/dev/versioning.md).

## Versioning & releases

Semantic Versioning + Conventional Commits + Keep a Changelog — see
[`CHANGELOG.md`](CHANGELOG.md) for what shipped and
[`docs/dev/versioning.md`](docs/dev/versioning.md) for the full
convention and how to cut a release with `scripts/release.py`.

## License

Licensed under the [GNU Affero General Public License v3.0](LICENSE)
(AGPL-3.0). Because AGPL is copyleft with a network-use clause, anyone
who runs a modified version of this app as a network service must also
make that modified source available to its users.
