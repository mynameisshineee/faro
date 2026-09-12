# Security

## What this software touches

`llminbox` can read/index markdown coordination files and can run the native
coordination journal. Both surfaces are served over HTTP on loopback. Two things
follow from that:

- **The content it serves is your coordination record.** For most teams that is
  operational detail — decisions, findings, infrastructure notes. Treat access to
  the port as equivalent to read access to those files.
- **It is written by LLMs and read by LLMs.** Entry bodies are untrusted text. The
  API marks them (`X-Llminbox-Untrusted`) and the inbox prefixes them with a notice,
  but nothing can stop a downstream agent from following instructions it reads. If
  you pipe `llmi inbox` into another agent's context, that agent must treat the
  content as data, never as instructions.

## What is enforced

| | |
|---|---|
| Legacy auth | Legacy data routes require `X-Llminbox-Token`. Without the configured token the legacy surface starts **mute** rather than serving records. |
| Native auth | Native routes require a short-lived Bearer session. Principal, role, runtime instance, lane and exact capabilities come from the authenticated workload registration; request bodies cannot replace them. |
| Status probes | `/health`, `/live` and `/ready` are unauthenticated and return status, not coordination records. `/ready` is 503 until projection and native admission are ready. |
| Exposure | Binds to `127.0.0.1` only. |
| Writes | Legacy ledgers are mounted read-only by default. The validating writer (`POST /append`) is opt-in. Native mutations are journaled and capability-checked. |
| Schema | `/docs` and `/openapi.json` are disabled — they leaked the API map, including ledger names, without a token. |

## Security properties by mode

| Mode | Authority and identity | Failure behavior |
|---|---|---|
| **legacy** | Markdown is authoritative. A shared token protects the HTTP surface; actor and lane are caller-declared labels. | The derived index may be rebuilt and file tools continue while it is down. |
| **bridge** | Native sessions provide server-derived identity for native operations; legacy records remain compatible and unverified for migrated operational semantics. | Each path follows its own authority rule; legacy input cannot promote itself. |
| **native-required** | The native journal is authoritative for the operator-admitted lane/verb pair. Bearer sessions carry server-derived identity and exact capabilities. | Fails closed. An unavailable native runtime never silently falls back to an authoritative markdown append. |

The v0.9 pilot exercised the native-required event/outbox admission pair and its
rollback. It did not prove multi-node isolation, hostile-host isolation or all-lane
fleet concurrency. The full matrix is in
[`docs/GUARANTEES.md`](docs/GUARANTEES.md), and the evidence boundary is in
[`docs/V0.9-CERTIFICATION.md`](docs/V0.9-CERTIFICATION.md).

## What is NOT enforced in the legacy bridge, and you should know

- **Loopback is not isolation on Docker Desktop for macOS.** Verified: a container on
  an unrelated network reached the published port through `host.docker.internal`.
  That is why the token exists and why it is mandatory, not optional.
- **The token is visible in `docker inspect` and in the container environment.**
  Anyone who can run Docker commands on the host can read it.
- **`actor` is self-declared for the shared token.** A caller presenting the shared token
  declares its own subject and nothing verifies it: the call is recorded and served. A
  caller presenting a per-agent credential does not: the subject is taken from the
  credential, and writing as another agent is refused with `403` on the legacy plane too.
  The remaining gap is therefore the shared token, not the legacy plane, and its closing
  condition is measurable — `/health` reports `v8.sin_identidad_24h`; closing it means no
  longer granting these verbs to the shared token, which is a code change, not a switch. `roster.json` has an empty `clave` field per agent — that is the
  reserved place for signatures if you ever need the ledger to serve as evidence
  rather than coordination. It is not implemented.
- **Lanes are not a boundary. Any caller with the token reads every ledger.**
  `--carril` / `BIK_CARRIL` scope *consumption* (whose cursor advances), never
  *access*. Measured on a real 12-ledger, ~69-agent installation on 2026-08-22 —
  same token, three requests for another lane's ledger:

  ```text
  X-Llminbox-Carril: <own lane>    → 200
  X-Llminbox-Carril: <other lane>  → 200
  no header at all                 → 200
  ```

  `/entries?ledger=` does not consult the header at all. There is no request that
  returns 403 for a lane you are not in, because that check does not exist. If you
  need a lane to be a wall, this is not the tool — and adding the header to a
  request does not make it one.

- **`X-Llminbox-Carril` is a scoping hint, not an identity.** The caller fills it
  in. It prevents *accidents* — draining the wrong lane's cursor — which is worth
  having, but for the shared token it is caller-declared and nothing verifies
  it, in the same way as `actor`.

- **One shared token means the service cannot tell its callers apart.** There is a
  single `LLMINBOX_TOKEN`; every agent presents the same secret, so "which agent is
  asking" is not a question the service can answer. Per-agent authorization is
  therefore not something you can configure on that surface. Native sessions remove
  this ambiguity for native routes; they do not retrofit legacy routes.

  On the reference installation the token file is `0600`, which sounds like
  isolation and is not: **all agent sessions run as the same OS user**, so the mode
  bits separate them from other accounts on the machine, not from each other.

- **Before treating any of this as a vulnerability, measure the alternative path.**
  On that same installation, every ledger is `-rw-r--r--` and owned by the user the
  agents run as, so a process that can call the API can also `cat` the file. The
  service exposes nothing the caller could not already read, and the honest
  conclusion is *"no isolation between agents at the host level"*, not *"llminbox
  leaks"*.

  **That conclusion is about that deployment, not about this software.** If you run
  agents in containers, under separate users, or with per-agent mounts — so that a
  caller can reach the port but *cannot* open another lane's file — then the shared
  token makes this service the bridge between those domains, and that is a real
  finding. Re-measure both halves before concluding either way.

- **`flock` only protects writers that take it.** An agent appending with `>>` does
  not. Measured: an entry written across several shell commands *will* interleave
  with another agent's — 8 of 16 bodies landed under the wrong header in a test with
  realistic delays. Write an entry in one command, or use the validating writer.

## Native runtime boundaries

- **Workload identity is authenticated, not self-declared.** The bootstrap/operator
  credential creates a short-lived session bound to principal, role, runtime
  instance, lane and exact capabilities. `/native/v1/whoami` reports that binding.
  Session expiry and revocation are enforced server-side. Do not place operator or
  workload secrets in ledgers, command lines, images or source control; mount the
  deployment-local secret files with the ownership and mode required by the image.
  **The browser is a fifth place, and the bundled web UI uses it.** A credential pasted
  into the UI is kept in `localStorage`: it survives closing the browser until you remove
  it with the UI's own control, while the short-lived session it opens is held in memory
  only. Treat a browser profile holding that credential as equivalent to the credential
  itself.
- **Native frame integrity uses HMAC, not a bare hash.** It authenticates bytes with
  a symmetric key inside the deployment trust domain. It is not an end-user digital
  signature and does not provide non-repudiation; a holder of the relevant symmetric
  key can produce a valid frame.
- **Admission is an operator action.** Clients cannot open their own lane. In v0.9,
  `events.accept` and `outbox.requeue` transition as one CAS-protected pair. Closing
  stops new admission; draining resolves pending/failed/unresolved work; sealing is
  terminal. Rollback certification is recorded atomically and is required before
  removing the native overlay. The executed sequence is in
  [`docs/V0.9-CERTIFICATION.md`](docs/V0.9-CERTIFICATION.md).
- **Capabilities and lane filters are boundaries only inside the service.** They do
  not protect against an attacker who controls the process, Docker daemon, database
  files or credential mounts. v0.9 is a single-node SQLite deployment, not a hostile
  multi-tenant host or Byzantine system.
- **Receipts prove admission and recorded lifecycle, not an exactly-once external
  effect.** Projection, telemetry export and integrations may be delivered at least
  once. Consumers must be idempotent; a receipt is not proof that a third-party
  business operation happened exactly once.
- **The native journal is durable authority and must not be deleted as a cache.** A
  certified return to the base compose preserves it. Physical volume loss,
  corruption and restore-from-backup were not certified by the v0.9 pilot.

## Your data, and what this software does not do with it

- **Coordination records are frequently personal data.** An entry names who wrote
  it and who it is addressed to, and a roster maps each agent to the person
  accountable for it. If you index records that identify people, you are the one
  processing that data: the legal basis, the retention period and the answers to
  subject requests are yours, not this project's.
- **"Append-only" does not mean you cannot withdraw an entry.** An earlier version of
  this file said this software does not implement selective erasure, full stop. That
  was wrong in the direction that matters, so here is the accurate version:
  - Your ledger is **your markdown file**. Removing an entry needs no feature from
    this project — you edit the file with the tools you already use.
  - The index does not silently forget: the row is **marked absent with the moment it
    disappeared**, and `llmi verify` reports it. Removal is made visible, not
    prevented — a record that can lose entries unnoticed is worse than one that
    cannot lose them at all. The consequence: a legitimate withdrawal and an
    accidental loss look the same to the integrity check until a human says which.
  - The **native journal** is the part that genuinely constrains erasure, because a
    receipt has to still mean something later. **Its retention policy is not written
    yet**, and until it is, do not point a graduated lane at records you may be
    obliged to erase. The gap is described in
    [`docs/GUARANTEES.md`](docs/GUARANTEES.md#withdrawing-an-entry-and-what-append-only-actually-constrains).
- **No telemetry by default.** Nothing about your ledgers, your agents or your usage
  is sent anywhere. There are no accounts and no cloud component. The implemented
  OpenTelemetry-compatible export is opt-in, carries runtime lifecycle and
  coordination signals only, and prompt and tool content stay out of it by default — and pointing an exporter at
  a third-party collector makes that provider a processor of whatever it receives,
  which is a decision for you to take deliberately.

## Reporting a vulnerability

**Please do not open a public issue for a suspected vulnerability**, and please do
not use an address you found in commit metadata: that is a historical record of who
typed something, not a monitored channel.

Use [Faro's private vulnerability reporting form](https://github.com/mynameisshineee/faro/security/advisories/new)
(Security → Report a vulnerability). Private vulnerability reporting was enabled
and its configuration verified on 2026-09-12. End-to-end delivery of a test report
has not yet been verified. If the form is unavailable, contact the maintainers
through an existing private channel and do not post vulnerability details publicly.

Include what you did, what you expected, and what happened. A proof of concept is
welcome; so is a report you are unsure about. **We would rather receive a report
that turns out to be a non-issue than not receive one.**

Before reporting, it is worth reading *What is NOT enforced* above: several
properties that look like vulnerabilities are documented limits with a measured
alternative path, and the honest conclusion is usually about a deployment rather
than about this software. Reports that come with both halves measured are the most
useful thing we receive.

### Scope

| In scope | Out of scope |
|---|---|
| Legacy or native auth bypass; reading a mounted ledger without the required credential; acquiring a Bearer session or capability for another workload; bypassing native lane/capability enforcement; opening admission as a client; stale-fence mutation; escaping the entry parser to forge a legacy header; consuming another caller's cursor outside the documented mode | The documented limits above (loopback on Docker Desktop for macOS, credentials readable by a Docker/host administrator, legacy lanes not being an access boundary, `actor` self-declared for the shared token, `flock` not binding shell appends, no exactly-once external effects) |

### What to expect

This is a small project, and pretending to an enterprise response time would be a
worse answer than saying so. Expect an acknowledgement within a few days, a fix or
a written decision once the report is understood, and credit in the release notes
unless you would rather not be named.
