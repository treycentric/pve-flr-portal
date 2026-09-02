# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
See [`docs/dev/versioning.md`](docs/dev/versioning.md) for how entries
here are generated from commit messages.

## [1.1.0] - 2026-09-02

### Added
- **timeline:** 5 fixed zoom levels, per-level ticks, callout semantics (#18) (a92d9e1)
- **restore:** push-to-guest restore - single-file, Direct Network Transfer, multi-file/directory bundles (#5/#22/#24) (5b9bdb8)

### Fixed
- **timeline:** keep the pill shape for the numbered callout (#18) (f40cd46)
- **timeline:** callout number is the position within its own tick group (#18) (7563a22)
- **timeline:** picker box covers the callout it opened from (#18) (ea2d658)
- **timeline:** stop tick labels overlapping, and enlarge the small callout (#18) (82d4dd9)
- **timeline:** keep end-of-month day labels at level 3 (#18) (8cfd6ec)

## [1.0.0] - 2026-08-31

Initial release: a functional Proxmox file-level restore portal.
Covers browsing and downloading one or more files/folders out of PBS
backups via Proxmox VE's `file-restore` API, a scrubbable snapshot
timeline across multiple guests, per-user PVE login, HTTPS by default,
and LXC/Docker deployment paths.
