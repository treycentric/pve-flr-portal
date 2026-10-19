# File Level Restore Portal for Proxmox Backup Server (pve-flr-portal) — Claude Code project notes

## What this is
A companion web app for Proxmox VE + Proxmox Backup Server that adds a
scrubbable snapshot timeline (in the spirit of Synology Active Backup for
Business's file-level restore browser) on top of Proxmox's existing
file-restore API. It does not modify Proxmox itself.

Architecture/reference docs, read the relevant one before writing code
in that area — all **living**, don't let them drift from reality:
- **`docs/architecture.md`** — system design, data model, auth/session/
  TLS mechanics, stack, scaling limits.
- **`docs/api-reference.md`** — the undocumented PVE file-restore and
  QEMU Guest Agent API contracts.
- **`docs/push-to-guest.md`** — restore-to-guest's current mechanism
  (capability detection, single-file/chunked/Direct Network Transfer
  write paths, multi-file bundles).

Companion docs, kept separate on purpose:
- **`TODO.md`** — open work (push-to-guest follow-ons, known
  limitations/tech debt).
- **`CHANGELOG.md`** — what shipped, per release (Keep a Changelog +
  SemVer; see `docs/dev/versioning.md`).
- **`docs/archive/`** — frozen historical record: `plan-phases-0-4.md`
  (how PH.0-PH.4 got built, including the timeline's
  `setPointerCapture`/`viewBox`/hit-area bugs worth knowing before
  touching `backend/static/app.js` again), `design-rationale.md` (why
  decisions were made), `push-to-guest-findings.md` (dated
  bug-investigation write-ups from building push-to-guest restore). Not
  living — don't edit; new lessons belong in the relevant living doc
  above, or a fresh dated entry in the matching archive file.

## Hard constraints
- This is a separate companion app, not an embedded plugin — Proxmox VE's
  web UI has no plugin system to embed into. Best effort first to design
  using baseline PVE capabilities; only reach for a patched/injected
  change to PVE/PBS's own files as a last resort, when nothing in PVE's
  published or undocumented-but-stable API surface can do it (issue
  #155's host-backup support is the first and so far only case — PVE's
  own `PBSPlugin.pm::list_volumes` hardcodes `vm`/`ct` and drops `host`
  backups before any permission check runs, confirmed live, not fixable
  from this app's side at all). When a patch is genuinely the only path:
  it must be **opt-in** (never applied silently), it must **refuse to
  apply rather than guess** when the target code doesn't match exactly
  what it expects (a PVE/PBS update changed something), and it needs its
  own re-apply-after-upgrade story (e.g. a dpkg hook) — this is a
  well-established pattern in the wider Proxmox community (several
  third-party PVE plugins patch core files and reapply after updates the
  same way), not something novel to this project, but it's still a
  meaningfully bigger commitment than anything else this app does and
  should stay rare.
- **PBS is required.** Everything hangs off Proxmox's File Restore
  feature, which per Proxmox is PBS-only — plain `vzdump` backups on
  dir/NFS/CIFS storage cannot be browsed via any API and are out of
  scope.
- **Stateless except two small SQLite files.** The snapshot list and
  any not-yet-cached directory listing are read live from the PVE API
  per request. The exceptions: PH.6's directory-listing cache
  (`dir_cache.sqlite3`, issue #109) and the restore-job history/log
  store (`job_history.sqlite`, issue #124) — both under `PFR_DATA_DIR`
  (issue #30), each a single file written from the request path, never
  a system of record, never a background job. See `docs/architecture.md`.
- The file-restore endpoints Proxmox's own GUI calls are **not** part of
  the published API reference. The core `file-restore/list` contract is
  captured in `docs/api-reference.md` — read it before touching this
  code path. If new gaps show up (auth, download/extract, edge cases),
  repeat the same capture-from-real-traffic approach and append the
  findings there rather than guessing.
- Scope split is intentional: browse + download is the core app.
  "Restore directly into the live guest" (push-to-guest, PH.5 — shipped
  in v1.1.0, see `docs/push-to-guest.md`) is a separate feature on its
  own `qemu-guest-agent` path with its own privilege model. Keep the two
  separate — don't fold restore logic into the browse/download path or
  gate one on the other.
- Auth is per-user PVE ticket login (`docs/architecture.md`) — there is
  no shared service token, and the app never talks to PBS directly (all
  backup listing goes through PVE's own API). A logged-in user's PVE
  ticket/CSRF token lives server-side in the session store and is never
  sent to the browser.
- Prefer the simplest thing that works for a single-admin internal tool:
  no build pipeline, no SPA framework, no extra services beyond the one
  backend process. Durable state, when a feature genuinely needs it,
  goes in `PFR_DATA_DIR` (issue #30) as a single small SQLite file
  written from the request path — never a background job, never a
  separate service.

## Stack (decided, see `docs/architecture.md` for why)
- Backend: Python, FastAPI
- Storage: otherwise stateless. `PFR_DATA_DIR` (issue #30) holds PH.6's
  directory-listing cache and the restore-job history store (schemas in
  `docs/architecture.md`) and is the provisioned location for any future
  small state
- Frontend: server-rendered HTML + htmx + Alpine.js
- Timeline widget: hand-rolled inline SVG (no charting library — nothing
  off the shelf fits "date axis, one dot per discrete event, drag to
  scrub, zoom")

## Layout
- `docs/architecture.md`, `docs/api-reference.md`, `docs/push-to-guest.md`
  — living architecture/reference docs
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
- `deploy/` — LXC install scripts + systemd unit, split by where they run:
  - `host/` (on the PVE node): `lxc-create.sh` (the installer),
    `lib/settings.sh` (the settings registry and dialogs shared by the
    installer and the management tool), `pfr-manage.sh` (the management tool,
    run from the container's checkout), `pve-flr-portal` (the thin host
    command installed in `/usr/local/sbin`), `pfr-roles.sh` (creates,
    inspects and deletes the PVE roles), `patch-pve-host-backups.sh`, and
    `install-host-tools.sh` (installs the host command on a host that doesn't
    have it)
  - `container/` (inside the LXC): `install.sh`, `update.sh`, the `certbot-*`
    scripts, the systemd unit template, the rfc2136 credentials example
  - the old flat paths `update.sh`, `lxc-create.sh` (forwarding stubs) and
    the unit template (a copy: a 1.x `update.sh` that is already running reads
    it after checking out the new release) are compatibility for existing
    installs and pasted one-liners - remove them in 3.0;
    `tests/test_deploy_layout_compat.py` covers both layouts. Anything reading a container's checkout or a GitHub ref looks in
    the new location first, then the old one.
  - `Dockerfile` / `docker-compose.yml` are at the repo root; the image doesn't
    contain `deploy/`.
- `tests/` — pytest + `node --test` + stylelint; see `tests/README.md`
- `.github/workflows/` — CI (every push/PR) and release (tag push) gates
- `run.py` — entrypoint (HTTPS bootstrap, then serves the app)
- `requirements.txt` — Python deps

## Conventions
- Keep the backend to one process.
- Any new dependency goes in `requirements.txt` with a one-line comment
  on why it's there.
- When an assumption turns out wrong during implementation (most likely:
  the real file-restore API shape), update the relevant doc under
  `docs/` in the same change — don't let it drift from reality.
- New functionality needs a test in the same change (`pytest` for
  backend logic, `node --test` for `app.js` component logic) — see
  `tests/README.md`. CI enforces this on every push/PR and again as a
  release gate.
- Commit messages follow Conventional Commits (`feat:`, `fix:`, etc.) —
  `scripts/release.py` derives version bumps and `CHANGELOG.md` entries
  from them. See `docs/dev/versioning.md`.
- Every commit cites the GitHub issue it addresses — `(#N)` at the end
  of the subject line. See `docs/dev/versioning.md`.
- **No ticket references outside the history docs.** Never write issue/PR
  numbers (`issue #52`, `#47`, `(#N)`) in code, comments, docstrings,
  tests, scripts, config examples (`.env.example`), `README.md` or the
  living docs under `docs/`. State the reason in words instead. Issue
  numbers belong only in commit subjects, PR bodies, `TODO.md`,
  `CHANGELOG.md` and `docs/archive/` (the design-rationale and findings
  "journals" that exist to record history and lessons learned). Before
  finishing a change, grep the diff for `#\d+` and `issue \d+`.
- No "confirmed live" / "verified live" / dated test-result remarks in
  code, script or config comments — features are assumed to be tested.
  Test evidence goes in the PR's test plan.
