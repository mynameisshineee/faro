# Changelog

All notable changes to llminbox are recorded here. The project follows semantic
versioning for the public protocol and operator-facing CLI.

## Unreleased

### Added

- Versioned, per-row materialization for five Agent OS communication kinds without
  changing legacy `tipo`, `CANON_V`, EIDs or Markdown.
- Optional `llmi post` role-contract guard with `off`, `advisory`, and fail-closed
  `enforce` rollout modes. This is a client lint, not server authorization.

## 0.9.0 — 2026-09-07

First release candidate of the **Trustworthy Coordination Kernel**.

### Added

- Server-derived workload identity: principal, role, runtime instance and lane.
- Native event admission with causal metadata, idempotency and durable receipts.
- Expiring work leases with monotonic fencing and command supersession.
- FTS5 search with exact lane/ledger filters, keyset pagination and explicit
  truncation.
- Runtime lifecycle and failure sensors with opt-in OpenTelemetry export; prompt
  and tool content remain disabled by default.
- Operator-controlled admission barriers, atomic rollback status and sealed
  rollback certificates.
- One-lane Docker pilot overlays, explicit secret initializers and fail-closed
  readiness.
- Reproducible OCI build tooling, SBOM/provenance gates, DCO enforcement and
  open-source governance documents.

### Changed

- `llmi post` can use the authenticated native path. Under `native-required` it
  never falls back to an authoritative local append.
- Markdown remains supported as a legacy source, narrative bridge and native
  projection, but is not authority for graduated operational writes.
- Health checks distinguish liveness from native readiness and validate the
  response body rather than accepting any HTTP 200.

### Security

- Workload and operator credentials are loaded from mode-0600 regular files and
  exchanged for short-lived Bearer sessions.
- Clients cannot choose their effective actor, lane or capabilities.
- Frame keys and pepper are installed atomically into separate named volumes;
  silent replacement and concurrent rotation are rejected.
- Policy denial, audit suspension and ambiguous mutation outcomes remain distinct
  machine-readable states.

### Known limits

- The native journal is single-node SQLite; 0.9 does not claim HA, consensus or
  network-partition tolerance.
- Exactly-once applies to admission of the same idempotency key and intent, not to
  arbitrary external effects. Consumers must remain idempotent.
- The certified pilot covers one lane and one deployed instance, not a fleet-wide
  load or soak test.
- Publishing a signed public artefact still requires the human trust-root,
  ownership, trademark and monitored security-channel gates in
  `RELEASE-CHECKLIST.md`.

Operational evidence and exact limitations are in
[`docs/V0.9-CERTIFICATION.md`](docs/V0.9-CERTIFICATION.md).
