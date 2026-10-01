# TODO

Open work. What already shipped is in [`CHANGELOG.md`](CHANGELOG.md);
the historical *how it got built* narrative (including debugging war
stories worth knowing before touching `backend/static/app.js`'s
timeline code) is archived at
[`docs/archive/plan-phases-0-4.md`](docs/archive/plan-phases-0-4.md).
Current architecture/reference docs live in
[`docs/plan.md`](docs/plan.md).

## PH.5 — Push-to-guest — SHIPPED (v1.1.0, #5/#22/#24)

Restore file(s)/directories directly into the *running* guest via
`qemu-guest-agent` (QGA). Released in v1.1.0. Covers: capability-detected
dual path (a single small `agent/file-write` call when it fits,
otherwise chunked scratch-write+concat via guest-exec), Direct Network
Transfer (the guest fetches large content itself over a configured data
NIC — issue #22) as a faster alternative to chunking, and full
multi-file/directory bundle restore with an embedded, guest-side-verified
checksum manifest (issue #24). Live-verified against a real Linux CT
(multi-item bundle via Direct Network Transfer) and a real Windows VM
(single-directory restore, zip-fallback + chunked write). Full
design/build/live-testing history is in `docs/plan.md` §7.5–§7.7 — each
real bug found along the way (directory double-nesting, disk exhaustion,
misleading progress display, timeouts, memory blow-ups, Windows quoting)
has its own "Real-world finding" entry there with the fix, test, and
commit.

### Push-to-guest follow-ons (open, not blockers)

- #25 — zero-buffer streaming bundle builder (vs. today's local-disk
  staging), tracked separately since staging-through-disk's cost
  (confirmed real live: a multi-hundred-MB selection can matter on a
  small LXC container) turned out to matter in practice, but Direct
  Network Transfer already fixed the bigger practical problem (upload
  speed) independently.
- #20 — **SHIPPED** for Linux/BSD: single-file restore now has its own
  "Restore original owner/permissions" checkbox, sourced from a second
  `file-restore/download?tar=1` call (confirmed live 2026-09-28 that
  PVE's tar output carries real uid/gid/mode, unlike the JSON listing
  API — docs/plan.md §7.5). **Windows ACLs confirmed infeasible** via
  any Proxmox-exposed API (investigated 2026-09-28, docs/plan.md §7.5)
  — neither tar nor zip has a field for an NTFS Security Descriptor,
  and no other Proxmox API surfaces it either; the checkbox is disabled
  for a Windows guest for exactly this reason. A real upstream Proxmox
  gap, not something fixable from this app.
- #26 — **SHIPPED** for Linux/BSD: same "Restore original owner/
  permissions" checkbox now works for multi-file/directory restore too.
  `restore_bundle.py`'s bundle builder now sources directory items via
  `tar=1` instead of the old default zip (Windows guests still use zip
  - Windows never needs uid/gid/mode, and zip's own per-entry timestamp
  already covers mtime with no format switch needed) and copies real
  uid/gid/mode/mtime onto each outgoing entry - no companion manifest
  needed, PVE's own archive metadata carries it directly. **Also fixed
  a separate, previously-unnoticed bug found while building this**:
  multi-file/directory restore never actually preserved original
  modified times despite the UI claiming it did (`TarInfo`/`ZipInfo`
  defaulted to epoch/"now" - fixed unconditionally, independent of the
  ownership checkbox). Windows ACLs remain infeasible, same reason as
  #20.
- #7 — `/api/download-bundle` (the plain browser download feature,
  separate from restore-to-guest) still buffers the whole archive in
  RAM; the streaming techniques #24 built are directly reusable there
  but haven't been applied yet.
- #47 — HTTPS on the Direct Network Transfer data plane. **SHIPPED** —
  live-verified (2026-09-08, Windows + Linux VMs, self-signed and
  step-ca certs) and merged into `main`. `verify`/`insecure`/`plaintext`
  policy + downgrade ladder (steps down on a runtime TLS-trust failure
  too), self-signed data-plane cert with IP SANs, HTTPS data listener,
  per-fetch-tool skip-verify, per-NIC `hostname`, minimum TLS version,
  automatic guest CA install, `PREFERRED` default `verify`, `curl.exe`
  preferred on Windows. Design + shakeout log: `docs/plan.md` §7.6.1.
  Deferred follow-ups: `openssl s_client` POSIX candidate;
  `UNINSTALL_CA_AFTER`; `bitsadmin` over HTTPS.
- #52 — bake certbot + a DNS-01 plugin into the LXC/Docker build, with a
  renewal deploy hook and a first-run helper. Design in the issue.

## PH.6 — Directory-listing cache — SHIPPED (issue #109)

A lazily-populated SQLite `dir_cache`, written on `/api/browse`
cache-miss (schema in `docs/plan.md` §6, `backend/dir_cache.py`). Keyed
by `(username, volume, path)` — not just `(volid, path)` as originally
sketched, matching `pve_client.list_path()`'s existing per-user
authorization threat model (a cache shared across users would let one
user see a listing PVE never actually authorized for them). Every
uncached `file-restore/list` costs ~3s (cold helper-VM boot); scrubbing
N snapshots in the same folder used to pay that N times. Single SQLite
file, no background job, per CLAUDE.md's "no extra services" constraint.

**No longer "optional, perf only" (2026-09-30):** issue #109 confirmed
PVE's own privileged API worker pool is a small (default 3), shared,
node-wide resource that this app's `file-restore/list` calls compete
for against every other operation on the node — a real host-instability
report traced back to it. `FILE_RESTORE_LIST_MAX_CONCURRENCY` (lowered
to 2) caps how many calls this app makes *at once*, but doesn't reduce
how *often* it needs to call PVE at all. Timeline scrubbing is
disproportionately revisit-heavy — dragging back and forth over
already-seen points is normal scrub UX — so a cache turns most of that
traffic into free local hits instead of new calls competing for one of
PVE's few shared worker slots. It does **not** fully replace the
concurrency cap: a first-time visit to N distinct new snapshots still
needs N live calls regardless of caching. Both mitigations matter
together.

**More motivated after #80:** hiding unmountable partitions/disks/LVM
volumes (main.py's `_filter_unmountable_children`/
`_disk_has_visible_content`) adds one extra `file-restore/list` call
*per child* on top of the listing itself, so expanding a disk with
several partitions in the tree can now take several seconds even on a
warm PVE. Live-reported (2026-09-28): 6-10s to expand a disk in the
tree view, previously near-instant. Worked around for now by making
the wait visible (`app.js`'s `_syncTreeToCrumbs`/
`_restoreTreeExpansion` now reuse the file grid's `#loading` indicator
during tree-sync fetches, since it wasn't shown there at all before and
made the slow-but-working case look identical to broken) rather than
by caching - this cache is the real fix for the underlying cost.

## Restore job retention & visibility — split from #121

Originally one issue, split into three on review (separately scoped:
viewing vs. cancelling vs. retention) plus a restart-reconciliation
requirement folded into the retention ticket:

- #122 — **SHIPPED.** Job list shows who submitted each job
  (`requested_by`), with an opt-in `RESTRICT_JOBS_TO_OWN` config
  (default off — unscoped, unchanged behavior) and a `BackupAdmins`-
  style bypass (`JOB_ADMIN_PRIVILEGE`, default `Sys.Audit`, checked via
  PVE's own `cap["dc"]` bucket — a privilege granted at the bare root
  path `/`, not scoped to any particular storage/VM). See
  `docs/plan.md` §7.5's "Job visibility scoping" subsection and
  README's "Provisioning access" → "Restore Job Visibility".
- #123 — **SHIPPED.** `POST /api/restore-jobs/{id}/cancel` now checks
  `job.requested_by == session.username` or `auth.is_job_admin(session)`
  (reusing #122's exact bypass check, not a separate privilege) before
  honoring a cancel — previously any logged-in user could cancel any
  job regardless of who submitted it. A non-owner, non-admin gets 403.
  The job list's per-job dicts also gained `can_cancel` so the UI can
  grey out the Cancel button proactively. See `docs/plan.md` §7.5's
  "Cancel ownership" subsection.
- #124 — open: persisted job + log history with a configurable
  retention window (`JOB_HISTORY_RETENTION_DAYS`, proposed default 7)
  in a dedicated `job_history.sqlite` under `PFR_DATA_DIR` (deliberately
  separate from PH.6's `dir_cache.sqlite`). Also needs restart
  reconciliation: any job still `queued`/`running`/`verifying` when the
  backend starts up must be closed out as `interrupted` rather than
  left looking perpetually in-progress, since today's in-memory
  `RestoreJobManager` loses all job state across a restart.

## Server-side per-user preferences store (follow-up to #29)

The colour theme (#29) persists per-browser in `localStorage` today, plus
an admin-wide `DEFAULT_THEME` env default. Making a user's choice
*follow them across browsers/devices* needs a small persisted
`{pve-username -> preferences}` store on the backend — a single JSON
file written from the request path (same "one file, no background job,
no service" shape as PH.6). The storage *location* is now sorted:
`PFR_DATA_DIR` (#30). Remaining is the feature itself — a
`preferences.json` under `config.ensure_data_dir()`, read at page
render and written from a small `POST /api/preferences` (or similar),
and the `docs/plan.md` §4 note that state is actually being written.
Deferred out of #29 deliberately to keep that change small.
**Still needs its own GitHub issue.**

## Known limitations / tech debt

Detailed in `docs/plan.md` §9.1 ("Scaling & limits") — condensed here
as an actionable list. None of these are correctness bugs; they're
ceilings the single-admin/single-worker design hits under load it
wasn't built for.

- [ ] **`/api/download-bundle` buffers the whole archive in RAM and
  compresses synchronously on the event loop** — a large multi-file
  selection can OOM the worker and stalls every other request
  (including auth) while it compresses. Single-file `/api/download` is
  unaffected (it streams). Fix: stream the archive as it's built; move
  compression to a thread via `run_in_executor`.
- [x] **No request coalescing/throttle on `file-restore/list` calls** —
  fixed in #60: `pve_client.list_path()` caps in-flight calls
  (`FILE_RESTORE_LIST_MAX_CONCURRENCY`) and coalesces identical
  concurrent requests per-user. Mostly moot on a cache hit now that
  PH.6's cache has landed (#109) — this still matters for first-time
  visits to new snapshots, which always need a live call regardless.
- [ ] **No pagination on huge directories** (Maildir, `node_modules`,
  WinSxS-scale folders) — full listing renders into one HTML partial
  and gets sorted/filtered entirely in JS. Fix: paginate or virtualize
  the grid past some row-count threshold.
- [ ] **`run.py` always passes `reload=True`** — fine for dev, wrong
  for a real deployment (extra file-watcher overhead, and `deploy/`'s
  systemd unit should own restart-on-crash, not uvicorn's reloader).
  Fix: gate `reload` behind an env var, default off.
- [ ] **One `httpx.AsyncClient` per PVE call, no connection pooling** —
  wasteful (fresh TLS handshake each time) but negligible at the scale
  this app runs at. Fix: one shared client instance.
- [ ] **`index()` reprocesses every archive on the datastore on every
  page load** — `list_backup_archives()` pulls the full list, then
  `index()` parses/groups all of it, uncached, per request. Fine at
  a single-admin deployment's scale; would matter on a busy shared
  datastore.
- [ ] **`renderTimeline()` tears down and rebuilds every SVG node each
  pan frame**, and `groupsInView()` walks all snapshots per frame —
  smooth at a few hundred dots, drops frames at multi-year retention
  (thousands). Fix: incremental DOM updates instead of full teardown,
  or windowing.
- [ ] **In-memory session store, single worker** (`backend/auth.py`) —
  can't run multiple uvicorn workers (each would have its own
  `_sessions` dict) or scale horizontally; a backend restart logs
  everyone out. Accepted tradeoff for now; would need session storage
  moved to disk to lift — PH.6 (#109) already added the SQLite
  infrastructure this could reuse, but sessions themselves aren't
  persisted there; that's still open, separate work.
- [x] **PVE 2FA/TOTP** — SHIPPED (issue #15). `auth.login()` raises
  `TFARequired` when PVE's `/access/ticket` response carries `NeedTFA`;
  `auth.finish_tfa_login()` does the second round-trip (the code goes
  in `password`, the intermediate ticket in `tfa-challenge`, per PVE's
  own source - confirmed against `PVE/API2/AccessControl.pm`, not
  guessed). `login.html` reveals a code-entry step in place, carrying
  username/realm/challenge as hidden fields - no server-side pending-
  login state needed. Covers TOTP and recovery keys (PVE accepts either
  the same way here); WebAuthn is out of scope (needs browser
  credential-API JS, real additional work). See `docs/plan.md` §7.1.
