# Tests

Run everything from the repo root:

```
./run-tests.ps1      # Windows (PowerShell) - primary
./run-tests.sh       # Linux / macOS / Git Bash
```

Subsets: `./run-tests.ps1 -Ruff -Python -Js -Css` (any combination) or
`./run-tests.sh ruff python js css`. Pass `-SkipInstall` / `--skip-install`
once deps are in place.

The runner bootstraps a `.venv` on first use (and `npm install` for the
CSS suite), then skips missing toolchains with a `SKIPPED` line rather
than failing. The JS suite has no npm dependencies.

Run one suite by hand:

```
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m pytest -q
node --test "tests/js/*.test.mjs"
npm run test:css
```

## Layout

| Suite  | Tool                     | Files                | What it covers |
|--------|--------------------------|----------------------|----------------|
| Ruff   | `ruff check` (`ruff.toml`) | `backend/`, `tests/` | lint: pyflakes, pycodestyle, import order, pyupgrade, ruff rules |
| Python | `pytest` + `respx`       | `tests/test_*.py`    | config parsing, PVE ticket auth / session lifecycle, the PVE API client (mocked httpx), `backend.main` helpers, every FastAPI route, TLS cert bootstrap |
| JS     | `node --test` (built-in) | `tests/js/*.test.mjs`| the Alpine component factories in `backend/static/app.js` — `taskPicker`, `userMenu`, `fileGridState` sorting/href building, `portalApp` timeline math |
| CSS    | `stylelint`              | `backend/static/*.css` | `.stylelintrc.json` — a focused "is this a bug" ruleset (invalid hex, unknown properties/units/pseudo-selectors, duplicate selectors/properties, malformed `calc()`), not the full cosmetic config |

## Notes

- `backend/static/app.js` ships as a plain browser script (no build step),
  so `tests/js/helpers.mjs` loads it by wrapping the source in a function
  that supplies the `window` / `document` / `htmx` globals.
- `pytest` must run from the repo root — `backend.main` mounts
  `backend/static` and `backend/templates` by relative path.
- `.tar.zst` bundle downloads use the `zstandard` PyPI package (not
  stdlib `compression.zstd`, which is Python 3.14+ only) specifically so
  the app runs on the deploy target's Python 3.11 — see
  `docs/plan.md` §10. Nothing here needs to skip itself by interpreter
  version.

## New functionality needs tests

Every suite above runs in CI on every push/PR (`.github/workflows/ci.yml`)
and is a required gate before a tagged release can publish
(`.github/workflows/release.yml`) — see docs/dev/versioning.md. New
backend logic gets a `pytest` test, new `app.js` component logic gets a
`node --test` test; a PR that only adds code without exercising it
through one of these suites isn't done.
