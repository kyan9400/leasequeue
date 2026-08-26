# Changelog

All notable changes are documented in this file.

## [1.0.0] - 2026-08-26

### Added

- Durable SQLite job storage with atomic priority-aware claims.
- Opaque, expiring lease tokens with heartbeat, completion, and failure commands.
- Bounded exponential retry scheduling and dead-letter redrive.
- Queue-scoped idempotency keys with request fingerprint conflict detection.
- Append-only transition timelines, operational metrics, health checks, and a browser console.
- Non-root container, Compose hardening, cross-platform tests, image scanning, and release automation.
