# Archived: design rationale and decision history

> **This is a frozen historical record**, not living documentation. It
> collects *why* a decision was made — alternatives considered and
> rejected — and dated bug-investigation write-ups that don't belong in
> a current-state reference doc. For how the system works today, see
> [`../architecture.md`](../architecture.md), [`../api-reference.md`](../api-reference.md),
> and [`../push-to-guest.md`](../push-to-guest.md). For push-to-guest's
> own bug-investigation history specifically, see
> [`push-to-guest-findings.md`](push-to-guest-findings.md). Not living
> — don't edit it.

## Why this project exists

Every backup in Proxmox VE's GUI is a separate, flat list entry. To
compare a file across two points in time you back out, pick a
different snapshot, and re-navigate to the same folder by hand.
Synology Active Backup for Business's restore portal has the piece
Proxmox lacks: a horizontal timeline along the bottom, one dot per
recovery point, that you drag or click to scrub through history while
the file listing above updates in place. That scrubber — and the small
service behind it — is what this project builds, on top of Proxmox's
existing file-restore API, without modifying Proxmox itself (it has no
plugin system to patch into anyway).

**PBS is a hard requirement, not just the common case.** The whole app
is built on Proxmox's "File Restore" feature, which per Proxmox's own
docs *"is only available for backups on a Proxmox Backup Server."*
Plain `vzdump` backups on directory/NFS/CIFS storage can't be browsed
file-by-file through any API — recovering a file from those is a
manual `vma extract` + loop-mount on the CLI, out of scope for this
app entirely. PBS also keeps many retained recovery points per guest
cheaply via dedup, which is what makes a *timeline* worth scrubbing in
the first place; a handful of rotated vzdump files would barely fill
one.

**Both VMs and containers, from the start.** The same `file-restore`
endpoint serves both (the volid's own path segment says `vm` or `ct`),
so there was never a reason to scope this to VMs only.

**Browse+download first; "restore into the live guest" as a
deliberately separate later phase.** ABB can write a restored file
straight back onto the source machine by connecting through VMware
Guest Tools and authenticating as a guest OS user with credentials the
operator supplies — it's not a Synology-specific agent. Proxmox's
file-restore API stops one step earlier: it hands you bytes, full
stop. Getting those bytes into a *live* guest's filesystem needs an
in-guest channel, and Proxmox already ships the equivalent of Guest
Tools: `qemu-guest-agent`, the same agent the backup path already uses
for fs-freeze. QGA can write files and run commands in the guest, as
root/SYSTEM, with no guest credentials required — authorization is a
per-user PVE privilege, not a guest password — so this didn't need a
bespoke listener daemon, just a careful design on top of QGA. That
design became push-to-guest restore (PH.5, shipped v1.1.0) — see
[`../push-to-guest.md`](../push-to-guest.md) for how it works today.

## Auth: real bugs found live, not in review

**PVE 2FA/TOTP, two real bugs, both only found by live-testing against
a real account (2026-09-30).** The second `/access/ticket` call kept
failing with a correct code. First cause: the code-entry form's hidden
`username` field echoed back the raw, client-typed username
reconstructed with the realm (e.g. `alice@pam`), instead of the exact
string PVE itself returned in the first response. This mattered because
PVE's challenge ticket is cryptographically bound to it —
`assemble_ticket($ticket_data, $aad)` with `$aad` = the *normalized*
username after PVE's own `lookup_username`, not whatever a
client-reconstructed `f"{username}@{realm}"` string happens to produce.
Any mismatch fails verification outright, independent of whether the
code itself is right. Fixed by carrying PVE's own returned username
through the hidden field verbatim instead of reconstructing it. A
concrete illustration of why "obviously equivalent" strings aren't safe
to interchange across a cryptographic boundary designed around one
specific, authoritative source.

Fixing that still didn't get a correct code accepted — **second bug,
same live test**: the `password` value on the second call needs a type
prefix, `totp:123456` or `recovery:<key>`, not a bare code. Confirmed
against PVE's own real web UI client (`TfaWindow.js`'s
`finishChallenge('totp:' + code)`), not documented anywhere in the
Perl API2 schema — its own parameter description for `password` gives
no hint a TFA response needs this. Without it, PVE silently rejects the
response regardless of whether the code is correct, confirmed live
against a real account that could log into the Proxmox web UI with the
same OTP. Fixed by prefixing based on shape: a recovery key is always
four hyphenated 4-hex-digit groups and never looks like a bare 6-8
digit number, so the choice is unambiguous without a second input
field.

## TLS: a real deployment tore its own cert in a restart loop

**Refined 2026-09-08.** The original self-signed-cert bootstrap had no
protection against a broken cert/key pair — iterating on local config
could leave a stale cert next to a fresh key (or vice versa), and
uvicorn's `create_ssl_context` raised a fatal `KEY_VALUES_MISMATCH` at
startup with no recovery path short of manually deleting the files.
Fixed by tagging auto-generated certs with their own marker
(`O = pve-flr-portal (auto-generated)`), writing the pair atomically
(`.tmp` + `os.replace`), and re-issuing a broken pair only when it's
confirmed to be one of this app's own — a broken admin-supplied cert is
logged as an error and left exactly as the operator left it, never
deleted or overwritten. The current behavior this produced is
documented in [`../architecture.md`](../architecture.md)'s TLS section.

## "Why 2, not 4" — the PVE worker-pool investigation (issue #109)

`FILE_RESTORE_LIST_MAX_CONCURRENCY`'s default of 2 (down from an
original 4) exists because of two independent live incident reports,
not a guess.

**First report (2026-09-30):** a user reported their PVE host
effectively crashing — unresponsive, possibly HA-fenced — while
scrubbing the timeline. Verified against Proxmox's own source
(`PVE/Service/pvedaemon.pm`, `PVE/Service/pveproxy.pm`): both hardcode
`max_workers => 3` as their default, and critically, this worker pool
is **shared node-wide** across every user and every privileged
operation, not scoped to this app or to file-restore specifically. The
original default of 4 could exceed PVE's entire pool on its own; even
exactly matching it at 3 would let this app alone claim 100% of the
node's API capacity for the ~3s+ duration of each cold helper-VM boot,
starving VM operations, other backups, and the PVE UI itself for that
window — a plausible mechanism for an apparent "crash" with no OOM or
panic involved (an HA-enabled cluster fences a node it can't reach via
the API). 2 guarantees at least one of PVE's three shared slots stays
free at all times.

**Second report, confirmed independently (issue #104, credit to
reporter `scyto`):** a precise root cause this project's own research
hadn't nailed down yet — `file-restore/list` is `protected => 1` in
`PVE::API2::Storage::FileRestore`, so `pveproxy` *always* hands it to
`pvedaemon` specifically, not "probably one of the two daemons" as
earlier research had hedged. Their reproduction: with the old default
of 4, all three of `pvedaemon`'s workers went busy servicing
file-restore listings, and an ordinary PVE login (`POST
/access/ticket`) then queued behind them — reported as "Proxmox logins
stopped working on the node set as PVE_HOST until the portal was
stopped." They ran with `FILE_RESTORE_LIST_MAX_CONCURRENCY=1` in
production and suggested "1, or at most 2" as the new default — this
project chose 2, the upper end of that range. Their report also
surfaced a previously-undocumented fact: every helper VM boots on
`PVE_HOST`'s node specifically, regardless of which node the actual
guest being browsed lives on — in a multi-node cluster, this
concentrates *all* file-restore traffic from every node onto one
node's `pvedaemon` worker pool.

The resulting mitigation (the concurrency cap, request coalescing, and
PH.6's directory-listing cache) is documented as current behavior in
[`../architecture.md`](../architecture.md)'s "Scaling & limits" section.

## Deployment: why LXC over a VM/OVA or a `.deb` package

Decided 2026-08-30, after packaging/deployment options were requested
given the hard PVE dependency.

- **Debian package installed directly on the PVE host** — ruled out.
  Installing arbitrary third-party packages on a PVE host risks
  colliding with Proxmox's own apt sources/dependencies, which the
  Proxmox community consistently advises against. Not worth the risk
  for a companion app that doesn't need to run *on* the hypervisor.
- **VM/OVA** — correct but heaviest option for what is a single tiny,
  mostly-stateless Python process: full guest-OS overhead, a slower
  build/update pipeline (rebuild an image vs. a git-based update), and
  "runs on any hypervisor" isn't a real benefit here since the target
  audience is, by definition, already running Proxmox.
- **LXC container (chosen, primary path)** — PVE-native, minimal
  overhead, matches how the Proxmox community already ships companion
  tools (the common `pct create` + install-script pattern). Fully
  isolated from the PVE host's own OS/package management.
- **Docker image (chosen, secondary path)** — covers people running
  Docker elsewhere entirely (Synology, TrueNAS, unraid, a separate
  Docker host) rather than wanting another PVE guest, and doubles as
  the fastest local dev/test loop.
- **`.deb` package / apt repo** — investigated and rejected (issue #89
  background research, 2026-09-29). Debian bookworm's packaged
  fastapi/uvicorn/cryptography are too stale to depend on directly, the
  only sound vendoring route (`dh-virtualenv`) is itself orphaned in
  Debian, and a real `.deb` would need a maintained signed apt repo —
  exactly the "extra service" this project avoids elsewhere. The LXC
  container already serves as the disposable, isolated install unit a
  `.deb` would otherwise buy.

**Release-pinned installs, not `main` HEAD (issue #89).**
`deploy/lxc-create.sh`/`deploy/update.sh` target the project's SemVer
git tags + GitHub Releases rather than floating on whatever commit
happens to be on `main` — replacing an old undocumented `git pull &&
systemctl restart` path with something that can pin to, or roll back
to, a version that actually shipped and passed CI, while adding no new
service or packaging step.

## A dev-environment Python version mismatch that masked a real bug

**Deployment target is Python 3.11** (Debian 12 bookworm's default),
not the dev machine's 3.14 — this mattered concretely once. The
`.tar.zst` bundle-download format was originally implemented against
Python 3.14's brand-new stdlib `compression.zstd` (PEP 784), which
doesn't exist on 3.11. Caught before shipping by verifying the whole
test suite plus a real `.tar.zst` round-trip against an actual 3.11
interpreter, not just 3.14 — switched to the `zstandard` PyPI package
instead, and `ruff.toml`'s `target-version` was correspondingly changed
from `py314` to `py311` so lint doesn't suggest syntax the deploy
target can't run. This is the same class of issue as
[`push-to-guest-findings.md`](push-to-guest-findings.md)'s `tar=1`
zstd-detection bug, where Python 3.14's native zstd support silently
absorbed a bug that only reproduces on 3.11 — a standing reason to
verify anything zstd-related against 3.11 specifically, not just
whatever version the dev machine happens to run.

## Host backups: three rounds to find the real blocker (issue #155, 2026-10)

The permission model looked complete from reading PVE's source alone
— `Datastore.Allocate`+`Datastore.Audit` should have been sufficient.
Live-tested against a real cluster with exactly that granted, host
backups still didn't show up anywhere: not in this app, not in PVE's
own GUI, not even for `root@pam`. That ruled out permissions entirely
and pointed at something in PVE's storage-listing code itself —
`PVE::Storage::PBSPlugin::list_volumes`'s `next if !($btype eq 'vm'
|| $btype eq 'ct')`, the thing now documented plainly in
[`../api-reference.md`](../api-reference.md).

The first patch — just that one line — still wasn't enough:
`pvesh get .../content --content backup` came back empty even after
patching and restarting `pvedaemon`/`pveproxy` on every node. Reading
further into the same function found a second, independent filter
right after the first: `next if $bid !~ m/^\d+$/;` — a host backup's
id is a hostname, never numeric, so this line alone would have
excluded it regardless of the first fix. A lesson worth generalizing:
finding one matching `next if`/filter line in a function doesn't mean
it's the only one — read to the end of the function before writing a
patch against it.

Once both lines were patched and host backups finally listed, two
Perl warnings appeared on every single query —
`Argument "<hostname>" isn't numeric in int`, from `list_volumes`
setting `vmid => int($bid)` unconditionally. Confirmed live to fire on
every query rather than being purely theoretical, so worth patching
too even though this app never reads that field.

Adding a sentinel comment to the first patched line (so the patch
script could recognize its own prior work later) briefly broke
re-running the script against already-patched nodes: an earlier
version of the script had left that line in its patched form
*without* the sentinel, and the updated matching logic treated "right
text, missing the sentinel" as a mismatch rather than "needs
upgrading." Fixed by modeling each line as a chain of every form it's
taken across script versions, oldest to newest, with a mismatch only
when a line matches none of its chain's known forms. That fix itself
briefly broke `--remove` in the other direction: the sentinel-bearing
text is a literal superstring of the pre-sentinel text, so checking
the shorter form before the longer one produced a false double-match.
Fixed by always preferring the longest (most specific) matching form,
regardless of which direction the chain is read in — a reusable
lesson for any future exact-match-or-refuse patch with more than one
historical "already applied" form to recognize.

No other known community script was found patching these same lines
(checked Proxmox VE Helper-Scripts/`community-scripts`, the old
`tteck/Proxmox` discussions, `ayufan/pve-patches`, and forum threads
about host backups not listing in PVE). The closest-looking forum
thread turned out to be about a different, ownership-permission issue
entirely — PBS's `DatastoreBackup` role only covering backups the same
PBS identity created, not a namespace-wide grant the way its ACL
inheritance might suggest. That's a real, separate gotcha and is
documented as current fact in
[`../api-reference.md`](../api-reference.md); it just isn't what that
forum thread's title made it sound like.

## Deployment: a guided installer, deliberately lighter than community-scripts (issue #91)

`deploy/lxc-create.sh` is interactive at a real terminal — inspired by
community-scripts.org's (Proxmox VE Helper-Scripts) guided-install UX,
but deliberately a much lighter version of it: colored status lines and
a Basic/Advanced whiptail flow, not their full multi-file `build.func`
framework (no SDN vnets, GPU passthrough, cluster-wide CTID validation,
or save-defaults diffing — overkill for a single-app tool; see the "no
extra services"/"simplest thing that works" rule in `CLAUDE.md`).
Getting pve-flr-portal actually *listed* on community-scripts.org is
tracked separately as issue #92 — that requires a completely different,
purpose-built script pair (root-run, `uv`-based, tarball-deployed, no
dedicated service user) submitted to their own intake repo, not a
retrofit of anything in `deploy/`; see that issue for the specific
conflicts with this project's architecture.

Every prompt is env-var-first (already-set variables skip their
question entirely) and every prompt is skipped outright with no TTY
attached, falling back silently to its default — so a fully scripted/
piped invocation behaves exactly as before this issue, and `whiptail`
(auto-installed via apt if missing) degrades to plain `read -rp`
prompts if unavailable. Basic questions (CTID, hostname, network,
container storage, and which PBS storage to browse) are asked
regardless, since they're unavoidable per-install choices; Advanced
(off by default) adds resource sizing, Direct Network Transfer + its
TLS policy, and an inline Let's Encrypt setup step (issue #52) that
runs `certbot-setup.sh` once the DNS plugin's credentials file is in
place. Resolved answers are written straight into the container's
`.env` (via a small Python patch script pushed in with `pct push`,
rather than hand-rolled `sed`, so JSON values like `RESTORE_DATA_NICS`
never need shell-delimiter escaping) instead of leaving that as a
manual post-install edit.

A companion `deploy/uninstall.sh` is tracked as issue #93 — container
teardown plus an opt-in cleanup of the `FileRestoreReader`/
`FileRestoreOperator` PVE roles and every ACL grant using them (safe to
find via `pveum acl list` precisely because those role names are unique
to this project; there's no recorded "what this install granted" to
replay otherwise, since roles/ACLs are a manual `pveum` step, not
something the app itself tracks — no database, per the top-level "no
database" rule).
