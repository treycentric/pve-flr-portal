# TODO

Open work. What already shipped is in [`CHANGELOG.md`](CHANGELOG.md);
the historical *how it got built* narrative (including debugging war
stories worth knowing before touching `backend/static/app.js`'s
timeline code) is archived at
[`docs/archive/plan-phases-0-4.md`](docs/archive/plan-phases-0-4.md).
Current architecture/reference docs live in
[`docs/plan.md`](docs/plan.md).

## PH.5 — Push-to-guest (#5, #22, #24 — built, live-verified, not yet merged to main)

Restore file(s)/directories directly into the *running* guest via
`qemu-guest-agent` (QGA) — done, on branch `feat/ph5-push-to-guest`,
not yet squash-merged to `main`. Covers: capability-detected dual path
(a single small `agent/file-write` call when it fits, otherwise
chunked scratch-write+concat via guest-exec), Direct Network Transfer
(the guest fetches large content itself over a configured data NIC -
issue #22) as a faster alternative to chunking, and full multi-file/
directory bundle restore with an embedded, guest-side-verified
checksum manifest (issue #24). Live-verified against both a real Linux
CT (multi-item bundle via Direct Network Transfer) and a real Windows
VM (single-directory restore, zip-fallback + chunked write). Full
design/build/live-testing history is in `docs/plan.md` §7.5-§7.7 -
each real bug found along the way (directory double-nesting, disk
exhaustion, misleading progress display, timeouts, memory blow-ups,
Windows quoting) has its own "Real-world finding" entry there with the
fix, test, and commit.

**Remaining before this is really "done":**
- [ ] Squash-merge `feat/ph5-push-to-guest` into `main` (paused at the
  user's request pending their own live testing - testing is now
  complete, ready whenever they say go).
- [ ] Version bump + `CHANGELOG.md` entry via `scripts/release.py` as
  part of that merge, per `docs/dev/versioning.md`.

**Deliberately deferred follow-ons, not blockers:**
- #25 — zero-buffer streaming bundle builder (vs. today's local-disk
  staging), tracked separately since staging-through-disk's cost
  (confirmed real live: a multi-hundred-MB selection can matter on a
  small LXC container) turned out to matter in practice, but Direct
  Network Transfer already fixed the bigger practical problem (upload
  speed) independently.
- #26 — restore original owner/group/permissions (not just mtime) for
  a multi-file/directory restore, via a companion manifest file
  alongside the checksum one.
- #20 — same ownership/permissions gap for the single-file restore
  path.
- #7 — `/api/download-bundle` (the plain browser download feature,
  separate from restore-to-guest) still buffers the whole archive in
  RAM; the streaming techniques #24 built are directly reusable there
  but haven't been applied yet.

## PH.6 — Directory-listing cache (optional, perf only)

A lazily-populated SQLite `dir_cache` keyed by `(volid, path)`, written
on `/api/browse` cache-miss (schema already sketched in
`docs/plan.md` §6). Every uncached `file-restore/list` costs ~3s (cold
helper-VM boot); scrubbing N snapshots in the same folder currently
pays that N times. The app is correct without this — it's purely "stop
paying the same 3s tax repeatedly." Single SQLite file, no background
job, per CLAUDE.md's "no extra services" constraint. Est. 1-2 days.

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
- [ ] **No request coalescing/throttle on `file-restore/list` calls** —
  drag-scrubbing the timeline can fire many cold lookups fast, each
  booting a helper VM on the PVE node; two users on different guests
  compounds it. Fix: cap in-flight calls, dedupe identical ones.
  (Mostly moot once PH.6's cache lands.)
- [ ] **No pagination on huge directories** (Maildir, `node_modules`,
  WinSxS-scale folders) — full listing renders into one HTML partial
  and gets sorted/filtered entirely in JS. Fix: paginate or virtualize
  the grid past some row-count threshold.
- [ ] **`run.py` always passes `reload=True`** — fine for dev, wrong
  for a real deployment (extra file-watcher overhead, and `deploy/`'s
  systemd unit should own restart-on-crash, not uvicorn's reloader).
  Fix: gate `reload` behind an env var, default off.
- [ ] **One `httpx.AsyncClient` per PVE call, no connection pooling** —
  wasteful (fresh TLS handshake each time) but negligible at homelab
  volume. Fix: one shared client instance.
- [ ] **`index()` reprocesses every archive on the datastore on every
  page load** — `list_backup_archives()` pulls the full list, then
  `index()` parses/groups all of it, uncached, per request. Fine at
  homelab scale; would matter on a busy shared datastore.
- [ ] **`renderTimeline()` tears down and rebuilds every SVG node each
  pan frame**, and `groupsInView()` walks all snapshots per frame —
  smooth at a few hundred dots, drops frames at multi-year retention
  (thousands). Fix: incremental DOM updates instead of full teardown,
  or windowing.
- [ ] **In-memory session store, single worker** (`backend/auth.py`) —
  can't run multiple uvicorn workers (each would have its own
  `_sessions` dict) or scale horizontally; a backend restart logs
  everyone out. Accepted tradeoff for now; would need session storage
  moved to disk (SQLite, if PH.6 lands) to lift.
- [ ] **PVE 2FA/TOTP not handled** — if a target user has a second
  factor on their PVE account, `/access/ticket` needs an extra
  round-trip the login flow doesn't do yet (`backend/auth.py`).
  Revisit if/when actually needed by a real user.
