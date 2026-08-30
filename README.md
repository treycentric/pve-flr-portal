# pve-backup-portal

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

**Status:** Phase 1 (MVP browse & download) in progress; Phase 2
(timeline UI) also underway. See the roadmap in docs/plan.md.

Note: PH.1–PH.3 authenticate with one shared service-account token on
each of PVE and PBS (setup commands in docs/plan.md §3). When PH.4
(per-user login) replaces this, tear down those service accounts —
see "Tearing down the service-account credentials" in docs/plan.md §3.

## Running it

```
python -m venv .venv
source .venv/Scripts/activate   # .venv/bin/activate on Linux/macOS
pip install -r requirements.txt

cp .env.example .env
# edit .env: fill in PVE_TOKEN_SECRET and PBS_TOKEN_SECRET from the
# pveum / proxmox-backup-manager token creation commands in docs/plan.md §3

uvicorn backend.main:app --reload
```

Then open http://127.0.0.1:8000/.
