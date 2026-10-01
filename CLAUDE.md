# File Level Restore Portal for Proxmox Backup Server (pve-flr-portal) — Claude Code project notes

## What this is
A companion web app for Proxmox VE + Proxmox Backup Server that adds a
scrubbable snapshot timeline (in the spirit of Synology Active Backup for
Business's file-level restore browser) on top of Proxmox's existing
file-restore API. It does not modify Proxmox itself.

Full rationale, architecture, and current-system reference (auth, TLS,
data model, stack, risks/scaling, deployment) live in `docs/plan.md` —
read it before writing code. It's the **living** doc; don't let it
drift from reality. Three companion docs, kept separate on purpose:
- **`TODO.md`** — open work (push-to-guest follow-ons, known
  limitations/tech debt).
- **`CHANGELOG.md`** — what shipped, per release (Keep a Changelog +
  SemVer; see `docs/dev/versioning.md`).
- **`docs/archive/plan-phases-0-4.md`** — frozen historical record of
  how PH.0-PH.4 actually got built, including debugging war stories
  (the timeline's `setPointerCapture`/`viewBox`/hit-area bugs) worth
  knowing before touching `backend/static/app.js` again. Not living —
  don't edit it; append new lessons to `docs/plan.md` instead.

## Current status
**Actively developed — deliberately no version number pinned here,
since it goes stale the moment the next release ships; see
`CHANGELOG.md` for the exact version history.** Browsing/downloading
files out of PBS backups via PVE's file-restore API, a scrubbable
multi-guest timeline, per-user PVE login (password or SSO/OIDC realm,
#56), HTTPS by default, LXC/Docker deployment, colour themes (#29),
multiple PBS storages/namespaces (#43), and **push-to-guest restore**
via `qemu-guest-agent` (PH.5, issues #5/#22/#24) — single-file, Direct
Network Transfer (HTTPS by default, #47), multi-file/directory
bundles, and "restore to original location" (#68) resolved from the
item's own path, including Windows drive-letter display matched to
the guest's real disk/partition layout via bus-address matching, not
just attachment-order guessing (#77). A partition, disk, or LVM volume
PVE's file-restore helper can't mount is hidden automatically instead
of surfaced as a dead end (#80), each with its own tree icon (#83).
See `CHANGELOG.md` for release-by-release detail and `TODO.md` for
what's left — every planned phase has shipped, including PH.6 (a
directory-listing cache, issue #109); everything open in `TODO.md` is
follow-on refinement.

**No database, except one small cache.** The app is otherwise
stateless — the snapshot list and any not-yet-cached directory listing
are read live from the PVE API per request. See `docs/plan.md` §4 for
why the originally-planned indexer/poll turned out unnecessary. The one
exception is PH.6 (issue #109): a lazily-populated SQLite
directory-listing cache under `PFR_DATA_DIR` (issue #30's designated
writable directory, provisioned by the systemd unit / a Docker
volume) — a single file written from the request path, never a system
of record, never a background job. See `docs/plan.md` §6. This doesn't
change the broader "no extra service" rule — it's a file, not a
service.

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
  "Restore directly into the live guest" (push-to-guest, PH.5 — shipped
  in v1.1.0, see `docs/plan.md` §7.5–§7.7) is a separate feature on its
  own `qemu-guest-agent` path with its own privilege model. Keep the two
  separate — don't fold restore logic into the browse/download path or
  gate one on the other.
- Auth is per-user PVE ticket login (`docs/plan.md` §7.1) — there is no
  shared service token, and the app never talks to PBS directly (all
  backup listing goes through PVE's own API). A logged-in user's PVE
  ticket/CSRF token lives server-side in the session store and is never
  sent to the browser.
- Prefer the simplest thing that works for a single-admin internal tool:
  no build pipeline, no SPA framework, no extra services beyond the one
  backend process. Durable state, when a feature genuinely needs it,
  goes in `PFR_DATA_DIR` (issue #30) as a single small file written from
  the request path — never a background job, never a separate service.
  SQLite is reserved for exactly this shape of need; PH.6's
  directory-listing cache is the one feature that's taken it so far.

## Stack (decided, see `docs/plan.md` §8 for why)
- Backend: Python, FastAPI
- Storage: otherwise stateless. `PFR_DATA_DIR` (issue #30) holds PH.6's
  lazily-populated directory-listing cache (schema in `docs/plan.md`
  §6) and is the provisioned location for any future small state
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
- `backend/` — FastAPI app: `main.py` (routes), `auth.py` (PVE ticket
  session store), `pve_client.py` (PVE API calls, `list_path`
  throttling/coalescing), `config.py` (env/settings), `tls.py`
  (self-signed cert bootstrap), `version.py`; push-to-guest restore
  (PH.5): `guest_agent.py` (capability detection), `guest_agent_lock.py`
  (per-vmid guest-exec serialization), `guest_browse.py` (restore
  destination browsing), `guest_original_location.py`
  ("original location" resolution, #68/#77), `guest_ca.py` (data-plane
  CA install), `restore_runner.py`/`restore_jobs.py`/`restore_bundle.py`/
  `restore_chunking.py`/`restore_network_pull.py`/`restore_download.py`;
  `templates/`, `static/`
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
- Every commit cites the GitHub issue it addresses — `(#N)` at the end
  of the subject line. See `docs/dev/versioning.md`.
