# pve-backup-portal — Claude Code project notes

## What this is
A companion web app for Proxmox VE + Proxmox Backup Server that adds a
scrubbable snapshot timeline (in the spirit of Synology Active Backup for
Business's file-level restore browser) on top of Proxmox's existing
file-restore API. It does not modify Proxmox itself.

Full rationale, architecture, UI mapping, data model, and phased roadmap
live in `docs/plan.md`. Read it before writing code — this file is just
the operating summary Claude Code should hold in context every session.

## Current phase
**Phase 0 — Recon, CLOSED (2026-08-29). Phase 1 — MVP browse & download,
in progress.** The real `file-restore/list` endpoint was captured from
live Proxmox VE GUI traffic (docs/plan.md §3): base64 filepath encoding
quirk, ~3s cold-lookup latency. The two remaining open items (API-token
auth, `file-restore/download` shape) were resolved by consulting
Proxmox's own published API schema rather than a second traffic capture
— both endpoints accept a scoped API token (`allowtoken: 1`) and need
only volume read access. Full details in docs/plan.md §3 and §10.

## Hard constraints
- This is a separate companion app. Do not attempt to patch or embed into
  the Proxmox VE web UI — it has no plugin system.
- The file-restore endpoints Proxmox's own GUI calls are **not** part of
  the published API reference. The core `file-restore/list` contract is
  now captured in docs/plan.md §3 — read it before touching this code
  path. If new gaps show up (auth, download/extract, edge cases), repeat
  the same capture-from-real-traffic approach and append the findings to
  docs/plan.md §3 rather than guessing.
- Scope split is intentional: browse + download is phases 1–3. "Restore
  directly into the live guest" (push-to-guest) is phase 4, a separate,
  later, open-ended effort requiring an in-guest agent. Do not fold the
  two together.
- The backend's PBS API token must be scoped to `DatastoreReader` on the
  specific datastore only (not an admin token), held server-side, and
  never sent to the browser.
- Prefer the simplest thing that works for a single-admin homelab tool:
  no build pipeline, no SPA framework, no extra services beyond the one
  backend process and SQLite.

## Stack (decided, see docs/plan.md §Stack rationale for why)
- Backend: Python, FastAPI
- Storage: SQLite — snapshot metadata + a lazily-populated directory
  listing cache (schema in docs/plan.md §Data model)
- Frontend: server-rendered HTML + htmx + Alpine.js
- Timeline widget: hand-rolled inline SVG (no charting library — nothing
  off the shelf fits "date axis, one dot per discrete event, drag to
  scrub, zoom")

## Layout (to be created as phases land)
- `docs/plan.md` — the full plan
- `backend/` — FastAPI app (phase 1+)
- `frontend/` or `backend/templates/` — htmx/Alpine templates (phase 1+)
- `requirements.txt` — Python deps

## Conventions
- Keep the backend to one process.
- Any new dependency goes in `requirements.txt` with a one-line comment
  on why it's there.
- When a phase's assumptions turn out wrong during implementation (most
  likely: the real file-restore API shape), update docs/plan.md in the
  same change, don't let the doc drift from reality.
