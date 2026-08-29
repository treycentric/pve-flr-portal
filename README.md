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

**Status:** Phase 0 (recon) — not yet implemented. See the roadmap in
docs/plan.md before starting any code.
