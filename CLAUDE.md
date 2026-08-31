# Proxmox File Level Restore Portal (pve-flr-portal) — Claude Code project notes

## What this is
A companion web app for Proxmox VE + Proxmox Backup Server that adds a
scrubbable snapshot timeline (in the spirit of Synology Active Backup for
Business's file-level restore browser) on top of Proxmox's existing
file-restore API. It does not modify Proxmox itself.

Full rationale, architecture, and current-system reference (auth, TLS,
data model, stack, risks/scaling, deployment) live in `docs/plan.md` —
read it before writing code. It's the **living** doc; don't let it
drift from reality. Three companion docs, kept separate on purpose:
- **`TODO.md`** — open work (PH.5 push-to-guest, optional PH.6 cache,
  known limitations/tech debt).
- **`CHANGELOG.md`** — what shipped, per release (Keep a Changelog +
  SemVer; see `docs/dev/versioning.md`).
- **`docs/archive/plan-phases-0-4.md`** — frozen historical record of
  how PH.0-PH.4 actually got built, including debugging war stories
  (the timeline's `setPointerCapture`/`viewBox`/hit-area bugs) worth
  knowing before touching `backend/static/app.js` again. Not living —
  don't edit it; append new lessons to `docs/plan.md` instead.

## Current status
**v1.0.0.** Browsing/downloading files out of PBS backups via PVE's
file-restore API, a scrubbable multi-guest timeline, per-user PVE
ticket login (no shared service token, no direct PBS access), HTTPS by
default, LXC/Docker deployment. See `CHANGELOG.md` for release-by-release
detail and `TODO.md` for what's next (PH.5 push-to-guest is the only
open phase; PH.6, a directory-listing cache, is optional/perf-only).

**No database.** The app is stateless — snapshot list and every
directory listing are read live from the PVE API per request. See
`docs/plan.md` §4 for why the originally-planned indexer/poll turned
out unnecessary, and §6 for the one piece of persistence still on the
table (deferred, optional PH.6).

**PBS is required.** Everything hangs off Proxmox's File Restore
feature, which per Proxmox is PBS-only — plain `vzdump` backups on
dir/NFS/CIFS storage cannot be browsed via any API and are out of
scope (`docs/plan.md` §2).

## Hard constraints
- This is a separate companion app. Do not attempt to patch or embed into
  the Proxmox VE web UI — it has no plugin system.
- The file-restore endpoints Proxmox's own GUI calls are **not** part of
  the published API reference. The core `file-restore/list` contract is
  captured in `docs/plan.md` §3 — read it before touching this code
  path. If new gaps show up (auth, download/extract, edge cases), repeat
  the same capture-from-real-traffic approach and append the findings to
  `docs/plan.md` §3 rather than guessing.
- Scope split is intentional: browse + download is the core app.
  "Restore directly into the live guest" (push-to-guest, PH.5 — see
  `TODO.md`) is a separate, later, open-ended effort requiring
  `qemu-guest-agent`. Do not fold the two together.
- Auth is per-user PVE ticket login (`docs/plan.md` §7.1) — there is no
  shared service token, and the app never talks to PBS directly (all
  backup listing goes through PVE's own API). A logged-in user's PVE
  ticket/CSRF token lives server-side in the session store and is never
  sent to the browser.
- Prefer the simplest thing that works for a single-admin homelab tool:
  no build pipeline, no SPA framework, no extra services beyond the one
  backend process (and, only if PH.6 lands, a single SQLite cache file
  written from the request path — never a background job).

## Stack (decided, see `docs/plan.md` §8 for why)
- Backend: Python, FastAPI
- Storage: none today (stateless). Reserved for a lazily-populated
  directory-listing cache only, if PH.6 is taken (schema in
  `docs/plan.md` §6)
- Frontend: server-rendered HTML + htmx + Alpine.js
- Timeline widget: hand-rolled inline SVG (no charting library — nothing
  off the shelf fits "date axis, one dot per discrete event, drag to
  scrub, zoom")

## Layout
- `docs/plan.md` — living architecture/reference doc
- `docs/dev/versioning.md` — SemVer/Conventional Commits/release process
- `docs/archive/` — frozen historical docs; don't edit
- `TODO.md`, `CHANGELOG.md`, `VERSION` — open work, release history,
  current version (single source of truth, read by `backend/version.py`)
- `backend/` — FastAPI app (`main.py`, `auth.py`, `pve_client.py`,
  `tls.py`, `version.py`, `templates/`, `static/`)
- `scripts/release.py` — changelog/version-bump/GitHub-release automation
- `deploy/` — LXC install scripts + systemd unit; `Dockerfile` /
  `docker-compose.yml` at the repo root
- `tests/` — pytest + `node --test` + stylelint; see `tests/README.md`
- `.github/workflows/` — CI (every push/PR) and release (tag push) gates
- `run.py` — entrypoint (HTTPS bootstrap, then serves the app)
- `requirements.txt` — Python deps

## Conventions
- Keep the backend to one process.
- Any new dependency goes in `requirements.txt` with a one-line comment
  on why it's there.
- When an assumption turns out wrong during implementation (most likely:
  the real file-restore API shape), update `docs/plan.md` in the same
  change — don't let the doc drift from reality.
- New functionality needs a test in the same change (`pytest` for
  backend logic, `node --test` for `app.js` component logic) — see
  `tests/README.md`. CI enforces this on every push/PR and again as a
  release gate.
- Commit messages follow Conventional Commits (`feat:`, `fix:`, etc.) —
  `scripts/release.py` derives version bumps and `CHANGELOG.md` entries
  from them. See `docs/dev/versioning.md`.
