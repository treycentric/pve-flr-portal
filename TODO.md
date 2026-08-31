# TODO

Open work. What already shipped is in [`CHANGELOG.md`](CHANGELOG.md);
the historical *how it got built* narrative (including debugging war
stories worth knowing before touching `backend/static/app.js`'s
timeline code) is archived at
[`docs/archive/plan-phases-0-4.md`](docs/archive/plan-phases-0-4.md).
Current architecture/reference docs live in
[`docs/plan.md`](docs/plan.md).

## PH.5 — Push-to-guest (stretch, open-ended)

Restore a file directly into the *running* guest via
`qemu-guest-agent` (QGA) — no bespoke in-guest daemon needed, PVE
already wraps the relevant QGA commands at
`POST /nodes/{node}/qemu/{vmid}/agent/{cmd}`. Full mechanism, hard
limits, and the two design options below are written up in the archive
(the investigation itself hasn't changed, just where it's filed):
[`docs/archive/plan-phases-0-4.md` §7.4](docs/archive/plan-phases-0-4.md).

**Before starting**, confirm against the real environment (nothing
below should be assumed from the write-up):
- [ ] Target guests have `agent: 1` set and `qemu-guest-agent` running.
- [ ] PVE version — 9+ has granular `VM.GuestAgent.*` privileges; PVE 8
  only has the coarse `VM.Monitor` and can't scope this tightly.
- [ ] Exact `agent/file-write` payload ceiling on the running PVE
  version (reports online range 40-60 KiB).
- [ ] Whether `guest-exec`/`guest-file-*` are blocked on target guest
  OSes (RHEL-family blocks by default; Debian/Ubuntu/Windows allow).

**Design A (first cut, ~3-5 days):** pure `agent/file-write`, small
files/directories, ≤40 KiB base64 chunks, `VM.GuestAgent.FileWrite`
only. Restored files land `root:root 0644` with a fresh mtime — UI
must say so plainly. Covers the actual common FLR need (a clobbered
config, a deleted document).

**Design B (later, opt-in, open-ended):** `file-write` bootstrap +
`guest-exec` pull the real file over the guest's own network — fast,
large-file capable, needs `VM.GuestAgent.Unrestricted` (≈ root on the
VM, much larger blast radius). Only pursue if Design A proves too
limiting.

**Authorization:** a separate, deliberate ACL grant
(`VM.GuestAgent.FileWrite` / `.Unrestricted`) — never folded into
`FileRestoreReader`. Browsing a backup must not imply writing into the
live guest. "Restore" stays disabled without that privilege on the
selected guest.

## PH.6 — Directory-listing cache (optional, perf only)

A lazily-populated SQLite `dir_cache` keyed by `(volid, path)`, written
on `/api/browse` cache-miss (schema already sketched in
`docs/plan.md` §6). Every uncached `file-restore/list` costs ~3s (cold
helper-VM boot); scrubbing N snapshots in the same folder currently
pays that N times. The app is correct without this — it's purely "stop
paying the same 3s tax repeatedly." Single SQLite file, no background
job, per CLAUDE.md's "no extra services" constraint. Est. 1-2 days.

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

## Housekeeping

- [ ] `deploy/lxc-create.sh`'s `pveam` template pin
  (`debian-12-standard_12.7-1_amd64.tar.zst`) will eventually go stale
  as Debian ships newer point releases — bump it periodically or make
  it discover the latest available `debian-12-standard_*` template.
