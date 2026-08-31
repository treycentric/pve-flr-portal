#!/usr/bin/env bash
# Runs the full pve-flr-portal test suite: Ruff (lint), Python (pytest),
# JS (node --test), CSS (stylelint). No args = run all.
# Pass any of: ruff python js css
set -u

cd "$(dirname "$0")"

want_ruff=0 want_python=0 want_js=0 want_css=0 skip_install=0 selected=0
for a in "$@"; do
  case "$a" in
    ruff) want_ruff=1; selected=1 ;;
    python) want_python=1; selected=1 ;;
    js) want_js=1; selected=1 ;;
    css) want_css=1; selected=1 ;;
    --skip-install) skip_install=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done
if [ "$selected" -eq 0 ]; then
  want_ruff=1 want_python=1 want_js=1 want_css=1
fi

declare -A result
overall=0

find_python() {
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info[0]==3 else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}

venv_py=""
# venv layout differs: POSIX uses bin/, Git Bash on Windows uses Scripts/
find_venv_py() {
  if [ -x .venv/bin/python ]; then echo .venv/bin/python
  elif [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe
  fi
}
if [ "$want_ruff" -eq 1 ] || [ "$want_python" -eq 1 ]; then
  venv_py=$(find_venv_py)
  if [ -z "$venv_py" ] && py=$(find_python); then
    echo "Creating virtualenv at .venv ..."
    "$py" -m venv .venv
    venv_py=$(find_venv_py)
  fi
  if [ -n "$venv_py" ] && [ "$skip_install" -eq 0 ]; then
    "$venv_py" -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt
  fi
  if [ -z "$venv_py" ]; then
    echo "No Python 3 found - skipping ruff/pytest."
    [ "$want_ruff" -eq 1 ] && result[Ruff]="SKIPPED (no interpreter)"
    [ "$want_python" -eq 1 ] && result[Python]="SKIPPED (no interpreter)"
  fi
fi

if [ "$want_ruff" -eq 1 ] && [ -n "$venv_py" ]; then
  echo; echo "=== Ruff (lint) ==="
  if "$venv_py" -m ruff check .; then result[Ruff]=PASS; else result[Ruff]="FAIL"; overall=1; fi
fi

if [ "$want_python" -eq 1 ] && [ -n "$venv_py" ]; then
  echo; echo "=== Python (pytest) ==="
  if "$venv_py" -m pytest -q; then result[Python]=PASS; else result[Python]="FAIL"; overall=1; fi
fi

if [ "$want_js" -eq 1 ]; then
  echo; echo "=== JavaScript (node --test) ==="
  if command -v node >/dev/null 2>&1; then
    # zero npm deps - the tests use only node:test / node:assert
    # Unquoted so the shell expands the glob into explicit file args -
    # Node's own --test glob support is version-dependent (confirmed:
    # works on Node 24, silently finds nothing on Node 20/Ubuntu CI).
    if node --test tests/js/*.test.mjs; then result[JS]=PASS; else result[JS]="FAIL"; overall=1; fi
  else
    echo "Node.js not found - skipping."; result[JS]="SKIPPED (no node)"
  fi
fi

if [ "$want_css" -eq 1 ]; then
  echo; echo "=== CSS (stylelint) ==="
  if command -v npm >/dev/null 2>&1; then
    [ "$skip_install" -eq 1 ] || { [ -d node_modules/.bin ] || npm install --silent; }
    if npm run --silent test:css; then result[CSS]=PASS; else result[CSS]="FAIL"; overall=1; fi
  else
    echo "npm not found - skipping."; result[CSS]="SKIPPED (no npm)"
  fi
fi

echo; echo "=== Summary ==="
for k in "${!result[@]}"; do printf '  %-8s %s\n' "$k" "${result[$k]}"; done
exit "$overall"
