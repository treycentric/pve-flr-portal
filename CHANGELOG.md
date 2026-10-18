# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
See [`docs/dev/versioning.md`](docs/dev/versioning.md) for how entries
here are generated from commit messages.

## [1.9.0] - 2026-10-12

### Added
- **restore-jobs:** show job owner, add mine/all visibility scoping (#122) (38f3f23)
- **restore-jobs:** persist job history with retention + restart reconciliation (#127) (a0a7ffe)

### Changed
- **restore:** rename design_a/design_b to file_write/guest_exec, retire "Design C" jargon (#146) (c2664da)

### Fixed
- **restore-jobs:** restrict job cancellation to owner or job admin (#123) (ab01313)
- **restore:** chmod quick-restored files and the guest CA anchor (#136) (5508536)
- **security:** block cross-site writes, bind OIDC callback, forbid framing (#135) (d7a16af)

## [1.8.0] - 2026-09-30

### Added
- **docker:** publish a production container image to GHCR (#107) (474c9b2)
- **image:** sign published container images with cosign (#111) (7699d0f)
- **auth:** support PVE 2FA/TOTP and recovery keys in the login flow (#15) (6ffe3b5)

### Fixed
- **deploy:** mark update.sh executable, matching install.sh/lxc-create.sh (#89) (06bc1e5)
- **pve-client:** lower concurrency default to 2, and ship PH.6's directory-listing cache (#110) (e323080)
- **frontend:** serve htmx and Alpine from /static instead of unpkg (#105) (2de5ad9)

## [1.7.0] - 2026-09-29

### Added
- distinct icon for partition/LVM-volume filesystem roots (#83) (592ce02)
- **deploy:** pin install/update to tagged releases instead of main (#90) (f616e8e)

### Fixed
- escape guest-controlled filepaths/names to prevent stored XSS (#101) (de8380f)

## [1.6.0] - 2026-09-25

### Added
- **pve-client:** cap and coalesce file-restore/list calls (#60) (e5520c3)
- **restore:** restore to original location for VM guests (#68) (677ca8a)

### Fixed
- sort tree subdirectories and use drive icons in the file grid (#63, #64) (73d9b1d)
- **restore:** recognize the elevated-LVM/flattened-part crumb shapes (#70) (4dd1f59)
- **restore:** clear ReadOnly/immutable attributes before overwriting (#72) (d7053b9)

## [1.5.0] - 2026-09-24

### Added
- **auth:** support OIDC/SSO realm login (#56) (89aadef)

### Fixed
- **restore:** resolve the guest's real node for guest-scoped PVE calls (#51) (4a02456)

## [1.4.0] - 2026-09-09

### Added
- **restore:** HTTPS on the Direct Network Transfer data plane - PR1 (#47) (ed9e1a8)
- **restore:** guest CA install + verify default for the Direct Network Transfer data plane (#47) (86c327d)

## [1.3.0] - 2026-09-08

### Added
- **storage:** support multiple PBS storages/namespaces (#43) (2b5ab02)
- **release:** automate the release PR and post-merge tag (#41) (#42) (0068500)

## [1.2.0] - 2026-09-02

### Added
- **theme:** light/dark/proxmox-dark colour themes with admin default (#29) (3c51f36)
- **deploy:** PFR_DATA_DIR - a provisioned directory for future app state (#30) (262ad87)

### Fixed
- **auth:** retry realm lookup and fall back to pam/pve, never an empty dropdown (#31) (ebb0d00)
- **auth:** log the user out on session expiry instead of failing silently (#27) (662c1c5)

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
