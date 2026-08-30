<#
.SYNOPSIS
  Runs the full pve-flr-portal test suite: Python (ruff + pytest),
  JS (node --test), and CSS (stylelint).

.DESCRIPTION
  With no switches, runs everything. Pass any of -Ruff / -Python / -Js / -Css
  to run only those. First run bootstraps a .venv and installs npm dev deps.

.EXAMPLE
  .\run-tests.ps1
  .\run-tests.ps1 -Python
  .\run-tests.ps1 -Ruff -Js -Css
#>
[CmdletBinding()]
param(
  [switch]$Ruff,
  [switch]$Python,
  [switch]$Js,
  [switch]$Css,
  [switch]$SkipInstall
)

# 'Continue', not 'Stop': native tools (pytest, node, npm) signal failure via
# exit code, which we check explicitly; EAP=Stop would turn their stderr
# chatter into terminating errors on PowerShell 5.1.
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
Set-Location $root

if (-not ($Ruff -or $Python -or $Js -or $Css)) {
  $Ruff = $true; $Python = $true; $Js = $true; $Css = $true
}

$results = [ordered]@{}

function Find-Python {
  $candidates = @(
    @{ Exe = 'py';      Args = @('-3') },
    @{ Exe = 'python';  Args = @() },
    @{ Exe = 'python3'; Args = @() }
  )
  foreach ($c in $candidates) {
    $cmd = Get-Command $c.Exe -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
      $v = & $c.Exe @($c.Args + @('--version')) 2>&1
      if ($LASTEXITCODE -eq 0 -and "$v" -match 'Python 3') { return $c }
    } catch { }
  }
  return $null
}

# -------------------------------------------------------- Python (venv) ----
$venvPy = $null
if ($Ruff -or $Python) {
  $py = Find-Python
  if (-not $py) {
    Write-Host "`nNo Python 3 interpreter found on PATH." -ForegroundColor Yellow
    if ($Ruff)   { $results['Ruff'] = 'SKIPPED (no interpreter)' }
    if ($Python) { $results['Python'] = 'SKIPPED (no interpreter)' }
  } else {
    $venv = Join-Path $root '.venv'
    $venvPy = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path $venvPy)) {
      Write-Host "Creating virtualenv at .venv ..."
      & $py.Exe @($py.Args + @('-m', 'venv', $venv))
    }
    if (-not $SkipInstall) {
      & $venvPy -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt
    }
  }
}

# ------------------------------------------------------------------ Ruff ----
if ($Ruff -and $venvPy) {
  Write-Host "`n=== Ruff (lint) ===" -ForegroundColor Cyan
  & $venvPy -m ruff check .
  $results['Ruff'] = if ($LASTEXITCODE -eq 0) { 'PASS' } else { "FAIL ($LASTEXITCODE)" }
}

# ---------------------------------------------------------------- pytest ----
if ($Python -and $venvPy) {
  Write-Host "`n=== Python (pytest) ===" -ForegroundColor Cyan
  & $venvPy -m pytest -q
  $results['Python'] = if ($LASTEXITCODE -eq 0) { 'PASS' } else { "FAIL ($LASTEXITCODE)" }
}

# ------------------------------------------------------------------- JS -----
if ($Js -or $Css) {
  $npm = Get-Command npm -ErrorAction SilentlyContinue
  $node = Get-Command node -ErrorAction SilentlyContinue
}

if ($Js) {
  Write-Host "`n=== JavaScript (node --test) ===" -ForegroundColor Cyan
  if (-not $node) {
    Write-Host "Node.js not found on PATH - skipping." -ForegroundColor Yellow
    $results['JS'] = 'SKIPPED (no node)'
  } else {
    # zero npm deps - the tests use only node:test / node:assert
    & node --test "tests/js/*.test.mjs"
    $results['JS'] = if ($LASTEXITCODE -eq 0) { 'PASS' } else { "FAIL ($LASTEXITCODE)" }
  }
}

# ------------------------------------------------------------------ CSS -----
if ($Css) {
  Write-Host "`n=== CSS (stylelint) ===" -ForegroundColor Cyan
  if (-not $npm) {
    Write-Host "npm not found on PATH - skipping." -ForegroundColor Yellow
    $results['CSS'] = 'SKIPPED (no npm)'
  } else {
    if (-not $SkipInstall -and -not (Test-Path (Join-Path $root 'node_modules\.bin'))) {
      & npm install --silent
    }
    & npm run --silent test:css
    $results['CSS'] = if ($LASTEXITCODE -eq 0) { 'PASS' } else { "FAIL ($LASTEXITCODE)" }
  }
}

# --------------------------------------------------------------- Summary ----
Write-Host "`n=== Summary ===" -ForegroundColor Cyan
$failed = $false
foreach ($k in $results.Keys) {
  $v = $results[$k]
  if ($v -eq 'PASS') {
    $color = 'Green'
  } elseif ($v -like 'SKIPPED*') {
    $color = 'Yellow'
  } else {
    $color = 'Red'
    $failed = $true
  }
  Write-Host ("  {0,-8} {1}" -f $k, $v) -ForegroundColor $color
}
if ($failed) { exit 1 } else { exit 0 }
