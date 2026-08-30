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

## Tests

```
./run-tests.ps1     # Windows (PowerShell)
./run-tests.sh      # Linux / macOS / Git Bash
```

Covers lint (`ruff`), Python (`pytest`), the `app.js` frontend logic
(`node --test`), and CSS (`stylelint`). See [`tests/README.md`](tests/README.md).

## License

Licensed under the [GNU Affero General Public License v3.0](LICENSE)
(AGPL-3.0). Because AGPL is copyleft with a network-use clause, anyone
who runs a modified version of this app as a network service must also
make that modified source available to its users.
