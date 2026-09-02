# Versioning and releases

This project uses [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`),
[Conventional Commits](https://www.conventionalcommits.org/) to derive
version bumps and changelog entries automatically, and
[Keep a Changelog](https://keepachangelog.com/) for `CHANGELOG.md`'s
format. None of this is bespoke — it's the same three conventions most
tools in this space (`standard-version`, `release-please`, `semantic-release`)
implement; `scripts/release.py` is a small dependency-free version of the
same idea, kept in-repo per CLAUDE.md's "no extra services" preference.

## Where the version lives

The `VERSION` file at the repo root is the single source of truth — a
bare `1.2.3`, nothing else. `backend/version.py` reads it at import time
and exposes `__version__`, which the About dialog displays. Scripts read
the same file. Nothing else should hardcode a version number.

## Commit message format

```
<type>[optional scope][!]: <description>

[optional body]

[optional footer(s)]
```

**Types**, and the `CHANGELOG.md` section they map to:

| Type | Changelog section | Counts toward a bump? |
|---|---|---|
| `feat` | Added | minor |
| `fix` | Fixed | patch |
| `perf` | Changed | patch |
| `refactor` | Changed | — |
| `revert` | Changed | patch |
| `security` | Security | — |
| `chore`, `docs`, `style`, `test`, `ci`, `build` | *(excluded — not user-facing)* | — |
| anything else (`type:` present but not one of the above) | Changed | — |

That last row is a deliberate fail-open default: an unrecognized-but-present
type still shows up under "Changed" rather than silently vanishing, so a
typo'd type (`imporvement:`) doesn't make a real change disappear from
the changelog. A subject with no `type:` prefix at all (most non-conventional
commits) is excluded entirely, same as the ignored types above.

**Breaking changes** — either put `!` right after the type/scope
(`feat!:` or `feat(auth)!:`), or add a `BREAKING CHANGE: <description>`
footer to the commit body. Either always recommends a **major** bump and
adds a "Breaking Changes" section at the top of that release's changelog
entry, regardless of the commit's type.

**Scope** is optional and free-form (usually a subsystem: `auth`,
`timeline`, `deploy`, ...). When present it's rendered in the changelog
as `**scope:** description`.

Examples:

```
feat(timeline): add drag-to-pan on the ruler
fix: correct off-by-one in tick spacing
feat!: drop the shared PVE/PBS service token

BREAKING CHANGE: per-user PVE ticket login replaces PVE_TOKEN_*/PBS_*
env vars entirely; see docs/plan.md §7.1 for the migration.
```

A commit that doesn't match this format (or uses an excluded type) still
gets recorded in git history as normal — it just doesn't show up in
`CHANGELOG.md` or influence the recommended bump.

### Cite the issue

Every commit must cite the GitHub issue it addresses — put `(#N)` at
the end of the subject line, so it stays inside the description
`scripts/release.py` copies into `CHANGELOG.md` and GitHub auto-links
it on the repo:

```
fix(auth): handle expired ticket refresh (#15)
feat(timeline): add drag-to-pan on the ruler (#13)
```

For a commit that closes the issue outright, add a `Closes #N` footer
too (GitHub closes the issue on merge; the subject-line `(#N)` alone
only links it). A commit with no corresponding issue (a typo fix, a
one-line doc tweak) is the rare exception — open an issue first for
anything more than that.

### Bump precedence

Exactly one rule applies, in this order, across *all* commits since the
last tag: any breaking change → **major**; else any `feat` → **minor**;
else any `fix`/`perf`/`revert` → **patch**; else **no bump recommended**
(the changes aren't user-facing — e.g. only `docs`/`chore`/`test`).

## New functionality needs tests

Every commit that adds backend logic needs a `pytest` test; every commit
that adds `app.js` component logic needs a `node --test` test. This
isn't just a suggestion — `.github/workflows/ci.yml` runs the full suite
(`run-tests.sh`) on every push/PR, and `.github/workflows/release.yml`
runs it again as a gate before a tagged release is allowed to publish.
See `tests/README.md` for the suite layout.

## Day-to-day: `scripts/release.py`

```
python scripts/release.py suggest
```
Read-only. Prints the commits since the last tag, the recommended bump,
and a preview of the `CHANGELOG.md` entry it would generate. Run this
any time to see where things stand — it changes nothing.

```
python scripts/release.py bump auto      # or: major | minor | patch
```
Updates `VERSION`, prepends the new section to `CHANGELOG.md`, and
commits (`chore(release): vX.Y.Z`) — all **locally**, nothing pushed.
Add `--dry-run` to preview the exact diff without touching anything.
`auto` uses the same bump-precedence rule as `suggest`; pick
`major`/`minor`/`patch` explicitly to override it (e.g. a deliberate
major bump for a docs-driven breaking change that no commit flagged
with `!`).

> **`bump` also creates a local annotated tag — do not push it.** `main`
> is protected: direct pushes are rejected and every change lands via a
> squash-merged PR, which rewrites the commit's hash. The version tag
> must sit on the commit that actually ends up on `main`, so the tag
> `bump` made (on the pre-merge commit) is a dead end — `git tag -d
> vX.Y.Z` it, and create the real tag on the squashed commit (below).
> Why the automation cares: the next release runs `git describe --tags`
> to find the previous version, and only tags *reachable from `main`*
> count — a tag left on an orphaned commit makes the next release
> double-count every commit since the version before it. (`chore` is an
> excluded type, so the release commit itself never lands in a changelog
> or a bump either way.)

Getting the release onto `main` and tagged:

```
V=v$(cat VERSION)
git branch "release/$V"           # move the chore(release) commit onto a branch
git reset --hard origin/main      # (only if you ran bump on main) put main back
git push -u origin "release/$V"
gh pr create --fill --base main   # open the PR, then squash-merge it
git tag -d "$V"                    # discard bump's dead-end tag

# after the squash-merge shows up on main:
git checkout main && git pull
git tag "$V" && git push origin "$V"   # <-- pushing the tag fires the Release workflow
```

Pushing the tag triggers `.github/workflows/release.yml`, which runs the
test suite and then publishes the GitHub release automatically. To do
that step by hand instead (or from any machine with `gh` installed and
authenticated), once the tag is on `main`:

```
python scripts/release.py release            # tag defaults to v<VERSION>
python scripts/release.py release --draft     # create as a draft first
python scripts/release.py release --dry-run   # print what would be run
```

This reads the matching `## [X.Y.Z]` section out of `CHANGELOG.md` and
passes it straight to `gh release create` as the release notes — the
same code path the GitHub Actions workflow uses, so local and CI
releases are always generated the same way.

## Full walkthrough

```
python scripts/release.py suggest                   # sanity check first
python scripts/release.py bump auto                  # or major/minor/patch
git show                                             # review the commit

V=v$(cat VERSION)
git branch "release/$V"                              # commit -> branch
git reset --hard origin/main                         # only if bump ran on main
git push -u origin "release/$V"
gh pr create --fill --base main                      # then squash-merge the PR
git tag -d "$V"                                      # drop bump's dead-end tag

git checkout main && git pull                        # pull the squashed commit
git tag "$V" && git push origin "$V"                 # fires the release workflow
```

The tag-push workflow publishes the GitHub release from `CHANGELOG.md`.
To publish it by hand instead, still push the tag first (CI and
`release` both expect `vX.Y.Z` to exist on `main`), then run
`python scripts/release.py release`.

## CI

- **`.github/workflows/ci.yml`** — runs `run-tests.sh` (ruff, pytest,
  `node --test`, stylelint) on every push and pull request. This is the
  general correctness gate, independent of releases.
- **`.github/workflows/release.yml`** — triggers on pushing a tag
  matching `v*.*.*`. Runs the full test suite first (`needs:` gate — a
  release cannot publish if tests fail), then runs
  `python scripts/release.py release --tag <the pushed tag>` to publish
  the GitHub release from `CHANGELOG.md`.

## Dependabot

`.github/dependabot.yml` sets `commit-message.prefix: "chore"` (and
`prefix-development: "chore"` for npm's devDependencies) with
`include: "scope"`, so every Dependabot PR/commit already looks like
`chore(deps): bump fastapi from 0.141.0 to 0.141.1` — valid Conventional
Commits syntax, no manual retitling needed for the common case. Without
this config Dependabot's default messages (`Bump fastapi from ...`,
no type prefix) don't match `CONVENTIONAL_COMMIT_RE` at all, so
`scripts/release.py` would silently drop every dependency bump instead
of filing it under `chore` on purpose.

`chore` is one of the intentionally-excluded types (see the table
above) — routine dependency bumps don't show up in `CHANGELOG.md` and
don't influence the recommended bump. That's deliberate, not a gap: if
a specific bump actually matters to users (patches a real
vulnerability, for instance), retitle the PR before merging — e.g.
`fix(deps): bump fastapi to 0.141.1 (CVE-2026-xxxxx)` — so it lands
under "Fixed" and recommends a patch bump like any other fix. Dependabot
can't know which bumps are user-relevant on its own; a human still
makes that call at merge time.

This works with either merge strategy: squash-merge uses the PR title
(generated from the same `commit-message` config) as the commit on
`main`; a regular merge/rebase preserves Dependabot's own commits,
which already carry the prefix.
