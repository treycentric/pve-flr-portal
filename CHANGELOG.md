# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
See [`docs/dev/versioning.md`](docs/dev/versioning.md) for how entries
here are generated from commit messages.

## [1.0.0] - 2026-08-31

Initial release: a functional Proxmox file-level restore portal.
Covers browsing and downloading one or more files/folders out of PBS
backups via Proxmox VE's `file-restore` API, a scrubbable snapshot
timeline across multiple guests, per-user PVE login, HTTPS by default,
and LXC/Docker deployment paths.
