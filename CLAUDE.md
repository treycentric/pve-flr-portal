# Proxmox File Level Restore Portal (pve-flr-portal) — Claude Code project notes

## What this is
A companion web app for Proxmox VE + Proxmox Backup Server that adds a
scrubbable snapshot timeline (in the spirit of Synology Active Backup for
Business's file-level restore browser) on top of Proxmox's existing
file-restore API. It does not modify Proxmox itself.

Full rationale, architecture, UI mapping, data model, and phased roadmap
live in `docs/plan.md`. Read it before writing code — this file is just
the operating summary Claude Code should hold in context every session.

## Current phase
**Phases 0-4 done (as of 2026-08-30).** PH.0-PH.3 (recon, browse/download
MVP, timeline UI, multi-guest + filter + polish — *not* the originally
planned cache/indexer, see below) are complete. PH.4
(per-user auth) landed 2026-08-30: PVE ticket login replaces the old
shared PVE/PBS API tokens entirely, and the PBS admin API is no longer
called at all — group/snapshot listing now comes from PVE's own
`storage/{storage}/content` endpoint (docs/plan.md §7.1-7.3). The app
now serves HTTPS by default (self-signed cert, admin-replaceable) on
port 8008 via `run.py`, with a configurable idle timeout.

**No database.** The app is stateless — snapshot list and every
directory listing are read live from the PVE API per request. The
`snapshots` table / scheduled PBS poll from the original plan is
obsolete (PH.4 removed all PBS access; docs/plan.md §4). A
directory-listing cache (SQLite, docs/plan.md §6) is the one piece of
persistence still on the table, deferred to the optional **PH.6**.

**PBS is required.** Everything hangs off Proxmox's File Restore
feature, which per Proxmox is PBS-only — plain `vzdump` backups on
dir/NFS/CIFS storage cannot be browsed via any API and are out of
scope (docs/plan.md §2).

Open phases: **PH.5** (push-to-guest, via `qemu-guest-agent` — no
bespoke daemon; docs/plan.md §7.4) and the optional **PH.6**
(directory-listing cache). Both are separate later efforts.

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
  directly into the live guest" (push-to-guest) is phase 5, a separate,
  later, open-ended effort requiring an in-guest agent. Do not fold the
  two together.
- Auth is per-user PVE ticket login (PH.4, docs/plan.md §7.1) — there is
  no shared service token anymore, and the app never talks to PBS
  directly (all backup listing goes through PVE's own API). A logged-in
  user's PVE ticket/CSRF token lives server-side in the session store
  and is never sent to the browser.
- Prefer the simplest thing that works for a single-admin homelab tool:
  no build pipeline, no SPA framework, no extra services beyond the one
  backend process (and, only if PH.6 lands, a single SQLite cache file
  written from the request path — never a background job).

## Stack (decided, see docs/plan.md §Stack rationale for why)
- Backend: Python, FastAPI
- Storage: none today (stateless). Reserved for a lazily-populated
  directory-listing cache only, if PH.6 is taken (schema in
  docs/plan.md §6)
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
