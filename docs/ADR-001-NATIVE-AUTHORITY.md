# ADR-001 — Native authority, identity and durable receipts

Status: accepted for implementation in v0.9 M1; amended 2026-09-06 by
coordination rulings D11b, D12, D14 and the audit-scope ruling. The latter
makes the audit guarantee conditional and records F-10 as an open M3/M4 gap.

Date: 2026-09-05.

## Decision

llminbox will add a native coordination journal in a dedicated
`coordination.sqlite` database. The journal is the source of truth for native
authority. Markdown remains a supported projection and permanent compatibility
bridge, but a direct Markdown append is never authoritative for a lane and verb
that the operator has moved to native mode.

The rollout unit is `(lane, verb)`. There is no global pilot switch and coverage
of every existing credential is not a prerequisite for moving one verb in one
lane. Configuration is operator-owned; a client cannot promote its own event to
authoritative.

## Identity model

Four concepts remain separate:

- `principal`: durable workload identity, derived from a deployment credential;
- `role`: authorization attribute of that principal;
- `lane`: isolation and routing boundary, also derived from the credential;
- `runtime_instance`: one server-issued session of one principal.

The credential map becomes additive:

```json
{
  "<credential>": {
    "principal": "backend",
    "role": "backend",
    "lane": "llminbox"
  }
}
```

During migration, a missing `principal` may be derived from `role`, but that
value is fixed against an opaque `credential_ref` the first time the credential
is accepted; adding an explicit principal later creates a new principal mapping
and never rewrites historical attribution. `/whoami` must expose
`principal_source: "derived_from_role"`; while any active pilot identity is
derived, `/health` exposes the identity graduation gate as failed. Coverage of
other roles does not change authorization behavior.

An authenticated process opens a session with `POST /sessions`. The deployment
credential is the bootstrap identity; the resulting short-lived session token
is the per-runtime credential required by the pilot. The server allocates an
opaque `runtime_instance`, stores only the token hash, and binds both to the
credential-derived principal, role, lane and credential-map generation. Host
name, PID, image and build metadata are untrusted annotations; none participates
in identity or authorization. A client-supplied principal, role, lane or runtime
instance is rejected rather than silently ignored.

Session refresh rotates the token without changing or widening identity. The
old token is invalid immediately. Expiry, explicit revocation, removal of the
bootstrap credential, or activation of a new credential-map generation revokes
the session before any further mutation. The gateway checks revocation and map
generation on every request. An authenticated local operator reload validates a
new attested snapshot, replaces credential references and increments the active
generation in one transaction; it does not require a process restart. Session
tokens are bearer credentials: possession is the runtime trust boundary in
v0.9, so theft cannot honestly be presented as a distinguishable second
process. Short TTL, hash-only storage, rotation and revocation bound the risk;
hardware or transport-bound proof of possession is a documented non-goal rather
than a fake guarantee.

The authorization middleware is capability-based, not a growing route-name
allowlist. Domain and operational mutations require a runtime session. Bootstrap
operations whose only capability is issuing or rotating a session authenticate
with the deployment credential and cannot write events, leases, commands or
other domain state.

`GET /whoami` accepts either credential or session authentication and has no
body or identity-related query parameters. With a session it returns the four
resolved fields, session expiry and mutation capabilities. With only a valid
deployment credential it returns the principal, role and lane, but no runtime
instance and no capability whose policy requires a session. Its response is a
projection of server state, never a reflection of request metadata.

## Native event contract

`POST /events` requires a valid runtime session and an `Idempotency-Key`.
The client supplies typed intent; the server supplies attribution:

```json
{
  "type": "message",
  "verb": "inform",
  "to": ["security", "cto"],
  "kind": "DELIVERED",
  "head": "candidate ready",
  "body": "...",
  "causes": ["evt_..."]
}
```

The server adds `event_id`, `receipt_id`, `principal`, `role`, `lane`,
`runtime_instance` and `occurred_at`. The accepted transition also records the
measured build SHA, runtime-file fingerprint and provenance state as
non-authorizing attestation. `occurred_at` is the server's acceptance timestamp
and is also rendered in the Markdown header; materialization time is recorded
separately. `event_id` is an opaque server-issued identity.
`receipt_id` identifies the event's receipt stream, while each immutable state
transition has its own `transition_id`. `entry_eid` does not exist at
`accepted`; it is added to the receipt stream at `materialized`. It is a hash of
projected entry content and is not an event identity: real ledgers contain
repeated hashes, including repeats within one ledger.

The idempotency scope is `(principal, lane, verb, key)`. For new records,
request-hash version 3 is computed from canonical JSON of the client intent,
effective ledger, causes, external causes and the canonical fenced resource (or
`null`). The fenced resource is part of the logical operation; spelling aliases
are normalized before hashing. Only the operational `fencing_token` is excluded
from the hash. It authorizes the first acceptance, but a replay does not
reauthorize or prove current lease ownership: replaying the same key, operation
and resource with a different token, including one obtained after takeover,
returns the original receipt and performs no new mutation. Reusing that key for
another resource is a `409` conflict, and a genuinely new attempt after
reacquisition requires a new idempotency key. In one `BEGIN IMMEDIATE`
transaction the server:

1. returns the original receipt for the same key and request hash;
2. rejects the same key with a different hash as `409` without mutation; or
3. inserts the event, accepted receipt, projection outbox item and idempotency
   record, then commits and returns `202 Accepted`.

`201 Created` would claim that the projected resource already exists. At
acceptance the journal durably owns the event, but Markdown may not contain it
and search may not expose it yet. A replay with the same idempotency scope and
matching version-3 request hash returns `200 OK` with `replayed: true` and the
original `event_id` and `receipt_id`.

There is no durable `in_flight` reservation outside that transaction. Database
contention has a bounded timeout and a retryable response; it cannot produce a
second event for the same key.

Historical hashes are not silently reinterpreted. Version 1 and version 2 keep
their original reconstruction formulas. A version-2 row can replay an exact
legacy request; if its token differs, the server cannot prove that the token was
the only difference and fails closed as `REPLAY_UNVERIFIABLE`, with no mutation.
A fenced request cannot replay a version-1 row whose formula omitted the fence.
New rows use version 3. The resource and token that authorized first acceptance
remain immutable audit evidence and are never overwritten by a replay.

Receipt state is explicit:

```text
accepted -> materialized -> indexed -> delivered
                           \-> failed
```

`accepted` means that the coordination journal durably owns the event. It does
not mean that Markdown contains it, search can see it, or a recipient consumed
it. Every transition appends a receipt record; transitions do not overwrite
history. The first HTTP response and every replay cite the same `event_id` and
`receipt_id`, with a response-only `replayed` flag.

The public receipt projection is typed and independent of storage column names:
it returns `receipt_id`, `state`, `subject_type` and `subject_id`. Event receipts
also return `event_id` as a compatibility alias equal to `subject_id`; non-event
receipts omit that alias, so a denial or command identifier can never be
presented as an event. Internal `principal_id`, timestamps and transition rows
do not enter this projection. Transition history remains a separate endpoint.

Every failed operational attempt by a known principal or session is accounted
for by an individual or aggregate audit record and cites its attributable
receipt only if this process remains alive and the audit transaction can write
and commit. These conditions are the entire extent of the guarantee: they are
stated wherever the guarantee is stated. Whenever an application response can
still be emitted after they stop holding, that response reports audit
suspension rather than leaving a reader to infer it. The exceptions are the
F-10 crash window below and `MUTATION_OUTCOME_UNKNOWN`, which is not a rejection
and cannot establish whether those conditions stopped holding. An unknown
credential cannot create authoritative
coordination rows: it increments a rate-limited, size-bounded security counter
keyed by a rotating credential fingerprint and emits a content-free sensor
event. Raw secrets are never stored. This prevents unauthenticated traffic from
exhausting the durable coordination database while leaving every attributable
operational failure auditable under the conditions above.

Audit suspension is declared, never inferred. The audit write uses a
transaction of its own after the effect transaction has committed or rolled
back. It is deliberately not atomic with an effect that may live outside
SQLite, and failure to audit never undoes an already committed effect. The
acceptance receipt is different: it shares the event transaction and is atomic
with it. The price of the deliberate split is that a denial/failure receipt can
be missing, and that missing case must be visible. Whenever the journal cannot
take the audit write, or the write is attempted and fails, the response carries
`audit_suspended: true` and omits `receipt_id` so it never asserts that a
receipt exists.

A `MUTATION_OUTCOME_UNKNOWN` response is permitted only when the substrate is
accredited and writable. It omits both `receipt_id` and `audit_suspended`
because the gateway cannot determine whether Core or the audit transaction
wrote before the failure became visible.

When the substrate is already accredited and writable, a credential that is
conclusively unknown is non-attributable and therefore creates no denial
receipt; that is not audit suspension. If substrate state prevents resolution
or auditing, the substrate failure takes precedence and the response declares
audit suspension. Journal-unavailable and failed-audit cases carry the same
public signal: the caller learns only that auditing is suspended. Public
`/health` exposes the same generic boolean while the condition holds. For a
coordination-substrate failure, public `/ready` exposes only
`name=coordination,state=not_ready`; neither surface distinguishes the
underlying substrate state. Other readiness dependencies remain governed by
the closed wire registry. Exact schema, pepper, volume, migration and journal
failure details are restricted to operator sensors that are not served on the
public HTTP listener and whose access is controlled by the deployment operator.

One window stays open in v0.9 and is not claimed to be closed. An accepted event
and its acceptance receipt remain atomic. A SIGKILL after an operational
transaction rolls back but before its rejection is audited can leave no record
of that rejection; if an external effect committed first, that effect can
remain without the later audit record. Nothing inside the dead process can
observe or report either case. Closing the gap requires a transaction spanning
effect and audit, which is unavailable while effects may live outside SQLite,
or a durable supervisor that outlives the process. It is recorded as falsifier
F-10 and owned by M3, where the supervisor emits runtime sensors, and M4, where
the one-lane pilot produces crash-recovery evidence.
Until F-10 closes, no published surface -- including but not limited to this
ADR, README, SECURITY, GUARANTEES, the claims matrix, `/health`, `/ready` and
CLI output -- may state the audit guarantee without all its conditions.

Authenticated rejection storms are bounded too. Below the configured
principal/reason quota, and subject to the audit conditions above, each denial
has its own immutable audit event. Above it, requests cite one durable aggregate
denial receipt per bounded time bucket while an in-place counter records the
number suppressed, again only under those conditions; they cannot force one
durable row per request. If those conditions fail and a response can still be
emitted, it declares audit suspension and omits `receipt_id`. Legitimate
accepted events and their compact idempotency mapping are retained together so
expiry can never turn a replay into a new event. Disk budgets apply backpressure
before exhaustion; ordinary reads remain available, public `/health` exposes
only a generic condition, and internal sensors expose the cause.

## Markdown projection and crash recovery

An outbox worker materializes accepted events. It reuses the existing locked
append implementation and writes exactly one framed block containing an
unambiguous server marker:

```markdown
### [backend -> security · DELIVERED] 2026-09-05T02:00:00Z — candidate ready
<!-- LLMINBOX-EVENT-BEGIN event_id=evt_... payload_sha=... -->
...
<!-- LLMINBOX-EVENT-END event_id=evt_... -->
```

The renderer may use the existing Unicode arrow and recipient separator; the
example above is ASCII only to make the framing obvious. Both markers live
inside the entry, after its parser-visible header; a later append must not change
the preceding entry's `entry_eid`. `payload_sha` is SHA-256 over the canonical
JSON event intent plus server attribution and `occurred_at`, excluding
materialization metadata and the frame itself. Search hides framing markers from
snippets.

Recovery searches by `event_id`, not by `entry_eid` or a remembered byte offset.
If a complete begin/end frame with the expected payload hash exists, the worker
marks it materialized without appending again. A byte offset is only a hint. If
the begin frame is incomplete, the worker never truncates or rewrites the
append-only ledger. The broken frame remains non-authoritative; under the same
lock the worker appends one complete projection and emits a repair transition
that cites the residue. A parser excludes incomplete frames from inbox delivery
and ordinary results; they remain visible only through repair diagnostics.

A marker is never evidence of authority by itself. Authority is resolved only
by an exact `event_id` and payload hash in the coordination journal plus its
materialization transition. A copied or hand-forged complete frame stays
searchable with `authority: false`; duplicate complete frames for one event are
reported as repair-required and never manufacture a second native event.

Only the pilot lane ledger is mounted read-write in the gateway. Agent
containers receive no ledger mount. Legacy narrative appends remain searchable
with `authority: false`; direct appends cannot acquire leases, acknowledge
commands or close authoritative work.

## Exclusive work and fencing

An active work owner is represented by one lease row per resource. Acquisition,
renewal, release and takeover use `BEGIN IMMEDIATE`. Every successful acquisition
or post-expiry takeover increments a monotonic `fencing_token`.

All authoritative mutations of the leased resource must carry its current
fencing token. A mutation with an older token is rejected as `409`, makes no
domain change, and produces a denial receipt subject to the audit conditions
above. If those conditions fail and a response can still be emitted, it
declares audit suspension and omits `receipt_id`. Expiry alone does not make an
old worker safe: fencing at the mutation boundary is what prevents a paused
worker from acting after a new owner takes over.

Renewal preserves the token for the same live runtime session. A different
runtime instance, including another session of the same principal, must acquire
or take over and receives a new token. At most one unexpired owner can exist for
a resource. Leases are the sole native ownership authority. Existing legacy
`claims` remain bridge/search records with `authority: false`; they neither
create nor invalidate a lease and are never consulted for native authorization.

The existing `POST /claim` and `POST /claim/cierro` endpoints are compatibility
surfaces, not a second ownership system. When `(lane, claim)` is bridge they keep
legacy behavior. When it becomes native or native-required, the endpoints map
to lease acquire/release in the coordination journal and stop writing the legacy
`claims` table; pre-existing rows remain queryable but cannot authorize native
work.

## Commands, causality and supersession

Commands carry `command_id`, `workstream_id`, `revision`, `causes` and optional
`supersedes`. For a workstream, only the highest accepted non-cancelled revision
may start execution. A delayed older command is recorded and receipted as
`superseded`; it is never executed.

Command receipts distinguish at least `accepted`, `received`, `executing`,
`succeeded`, `failed`, `cancelled` and `superseded`. This prevents a transport
receipt from being mistaken for work completion and preserves a causal chain
without loading the full ledger.

`POST /commands` returns `202 Accepted` only when the resulting command state is
`accepted`. A delayed lower revision that is durably recorded as `superseded`
returns `200 OK`: it was registered for causality and audit, but was not accepted
for execution. Both responses include the server-issued `command_id`.

Native `causes` and `supersedes` reference native event or command identifiers
and use foreign keys. Causality toward permanent bridge/legacy material uses a
separate typed `external_causes` reference containing ledger and `entry_eid`;
it is explicitly non-authoritative and has no foreign key into the native
journal. A bridge hash supplied as a native cause is rejected and receipted
subject to the audit conditions above; otherwise any response that can still be
emitted declares audit suspension and omits `receipt_id`.

## Storage and recovery boundary

`coordination.sqlite` is durable and separate from the rebuildable search/index
database. It contains principals, credential references, runtime sessions,
events, idempotency records, receipt transitions, projection outbox items,
leases and commands. Foreign keys are enabled on every shipped runtime
connection; WAL and a bounded busy timeout are configured explicitly. The
coordination database uses `PRAGMA synchronous=FULL`. A future offline migrator
may disable foreign keys only on its private connection before `BEGIN
IMMEDIATE`; all DDL, DML and guards remain in that one transaction, including
`foreign_key_check` before the version seal. After commit it restores and
verifies `foreign_keys=ON`, or closes the private connection without exposing it
to runtime. A versioned metadata row
(`durable_v`) is checked before serving mutations. The v0.9 pilot admits a
journal created directly at version 6, or an already-existing, fully accredited
version-6 journal. Startup first accredits the configured path, its durable
parent volume and readability without writing, then classifies it through a
read-only probe before any read-write SQLite open. Classification is ordered and
disjoint:

1. an unavailable or unsafe path (including an unreadable object, non-regular
   file, symlink or I/O failure) is refused;
2. an absent database on an accredited durable parent enters the crash-safe
   version-6 bootstrap below;
3. for an existing safe file, a missing, non-integer or non-positive seal is
   refused as indeterminate without interpreting schema;
4. a seal greater than 6 is refused as too new without interpreting its schema;
5. a seal from 1 through 5 is checked against that version's own manifest: a
   matching database is refused pending offline migration and a mismatch is
   refused as corrupt; and
6. a seal of 6 is fully accredited against its manifest, journal identity,
   pepper, lane and durable volume; a mismatch is refused, a writable match is
   opened for normal service, and a match whose only failed capability is
   writability may serve reads but refuses mutations.

A pre-existing empty or partial file therefore never becomes a fresh database
and is never initialized in place.

Fresh creation uses a sibling staging file on the same accredited volume. It
builds the complete schema and version seal, fsyncs the staged database,
publishes with an atomic no-clobber operation, then fsyncs the parent directory
before reporting success. Under two concurrent
initializers exactly one can publish; the loser discards its staging file and
reopens the winner through the normal accreditation path. After a crash at any
DDL, fsync or publish boundary, the destination is either absent or a fully
accredited version-6 database, never a partial journal.

Those exact states and operator actions are exposed only by internal sensors.
Public
responses retain the closed generic substrate failure and public `/ready`
reports only `name=coordination,state=not_ready`. Refusal happens before
readiness can become ready or any mutation of coordination storage; external,
content-free sensors may still durably report the refusal. Falsifiers snapshot
the database, WAL, SHM and lifecycle files and require them to remain
byte-identical.

As measured on 2026-09-06 in the scoped BIK deployment inventory, there is no
supported deployed native-journal population to upgrade for v0.9. Therefore the
pilot does not ship or automatically invoke an in-place legacy backfill. The
version-5-to-6 work remains a documented draft for a future offline migration,
not dormant runtime code. Any future migration must be transactional, start
from a quiesced WAL-consistent snapshot and finish all guards before changing
`durable_v`. This restriction concerns the coordination journal only: legacy
Markdown remains readable, searchable and exportable under the bridge rules
above.

The storage gate falsifies every branch above: unsafe/unavailable/I/O paths,
absent-to-v6 creation, crash cuts at DDL/fsync/publish, two racing initializers,
writable and read-only valid-v6 restarts, both healthy and malformed examples of
each version 1 through 5, an internally distinct too-new classification with the
same generic public failure, non-positive and missing/non-integer seals, empty
files, partial or corrupt schemas, unchanged legacy Markdown search, and
absence of runtime migration symbols or call paths.
Removing or moving the parent-directory fsync before publication is a required
mutant: process-level crash cuts alone do not prove directory-entry durability
after power loss.

Startup fails closed if `coordination.sqlite` is not on the configured durable
volume. The deployment gate recreates—not merely restarts—the container and
then proves that journal events, receipts, leases and commands survived.

Readability and writability are reported per store to internal sensors. A
writable journal with a read-only or unavailable search index may still accept
native events and durable
receipts; indexing remains pending and search reports degradation. A fully
accredited version-6 journal whose only failed capability is writability may
serve reads and rejects every mutation with one generic retryable response
instead of entering a restart loop. Accreditation includes the journal
identity, manifest, lane, durable volume and pepper. An unavailable, unsafe or
structurally unclassified journal does not promise state reads. The caller is
told that the mutation was refused and auditing is suspended, but not which
substrate state refused it. Public `/health` exposes only the generic suspension
boolean.
For a coordination-substrate failure, public `/ready` reports only
`name=coordination,state=not_ready`; other dependencies follow the closed
readiness registry. The fixed external literal lives in the wire contract; the
operational values, evidence and remediation detail for the normative state
partition above are exposed only by operator sensors. A read-only
Markdown target leaves the event accepted and the outbox retry/failure visible;
it never relabels the event delivered.

Existing corruption recovery for the search database must never delete or
recreate the coordination journal. Backup, restore, migration and rollback
procedures treat it as user data. Secrets and raw credential values are not
stored in either database.

Rollback is a protocol, not just an image tag. Before moving to an image that
cannot read the active `durable_v`, the operator must stop new native writes,
drain the outbox to zero, explicitly change each affected `(lane, verb)` policy
from `native-required` to `bridge`, snapshot the journal and only then recreate
the service. Shutdown refuses while the outbox is pending unless an explicit
emergency operation records that fact. An old image must never silently turn a
native-required write into prose. During bridge rollback, an older image starts
without any path or file descriptor capability for the version-6 journal: the
journal is retained outside that container and only legacy bridge surfaces are
enabled. A falsifier proves that the old process cannot open the journal and
that the journal, WAL, SHM and lifecycle inventory remain byte-identical.
Forward recovery with a version-6-capable image is the only path that restores
the journal before reactivating native policy.

## Compatibility and CLI behavior

`llmi post` selects behavior from the operator's `(lane, verb)` policy:

- bridge: validate and append locally, labelled non-authoritative;
- native: send through `/events` and print the server receipt;
- native-required: never fall back for that authoritative verb if the gateway
  is unavailable.

Narrative communication may remain bridge forever. Claims, leases, command
acknowledgements and authoritative closure use native-required mode in the
pilot. Availability is not improved by silently converting an authoritative
write into unauthenticated prose.

A `native-required` failure returns a distinct non-zero CLI status and names the
exact `(lane, verb)` policy that prevented fallback. There is no client-side or
environment-variable override to bridge; only an operator policy change through
the audited rollback or migration procedure can change the mode.

The CLI adds `llmi whoami`, session login/refresh, receipt lookup, lease
acquire/renew/release and command acknowledgement. It prints event and receipt
identifiers in machine-readable JSON when requested.

## Required falsifiers

M1 is not complete until these controls pass against both the API and a deployed
one-lane instance:

1. two principals with the same role remain distinguishable;
2. request fields and headers cannot alter `/whoami`;
3. two sessions of one principal have different runtime instances;
4. a lane mismatch writes zero bytes to every ledger;
5. the same idempotency scope and version-3 hash (intent, effective ledger,
   causes, external causes and canonical fenced resource) under at least 20
   competing processes produces one event and the original receipt; changing
   any hashed field produces `409`; replaying that same operation after
   reacquisition with a different fencing token still returns the original
   receipt and performs no new mutation;
6. killing the worker before append, during append and after fsync produces no
   duplicate authoritative event and an observable receipt transition;
7. repeated `entry_eid` values in legacy/bridge material do not confuse native
   recovery;
8. after takeover, an expired owner attempting a new operation with a new
   idempotency key and its old fencing token is rejected, while replaying a
   previously accepted operation returns its original receipt without mutation;
9. a delayed superseded command is receipted but not executed;
10. while the process remains alive and the audit transaction can write and
    commit, every rejected operational attempt by a known identity is accounted
    for by an individual or aggregate audit record and cites its attributable
    receipt, while an unknown-credential flood cannot grow durable storage
    without bound; with the journal unwritable, and separately with the audit
    write forced to fail, any application response that can still be emitted
    carries `audit_suspended: true`, omits `receipt_id`, public `/health`
    reports only the generic suspension state, and public `/ready` reports only
    `name=coordination,state=not_ready` for the substrate-failure arm;
11. copied and hand-forged complete frames, not just narrative appends, remain
    searchable with `authority: false` and change no native state;
12. deleting the rebuildable index and recreating the container preserves every
    native event, receipt, lease and command from the mounted journal;
13. appending event N+1 does not change event N's `entry_eid`, including after
    index rebuild;
14. withdrawing a bootstrap credential, rotating the map, refreshing a session
    or reaching expiry invalidates the prior token before another mutation;
15. the database stores no recoverable session token and a replay after
    revocation is denied and receipted subject to the audit conditions above;
    otherwise any response that can still be emitted declares audit suspension
    and omits `receipt_id`;
16. making `roles_cubiertos == roles_emisibles` changes no authorization result;
17. native leases and legacy claims cannot create two native owners;
18. rollback refuses a pending outbox and never silently downgrades a
    native-required write;
19. rotating the credential map without restarting the process denies an old
    session on the very next request;
20. a delayed projection renders the event's acceptance `occurred_at` in its
    header and records a distinct materialization time;
21. the lane-mismatch control snapshots size, modification time and SHA-256 of
    every mounted ledger before and after the request, rather than trusting only
    the HTTP denial;
22. rejection floods from both unknown and known identities stay within their
    durable row and disk budgets; a known-identity rejection returns an
    attributable receipt only while the process remains alive and its audit
    transaction commits, otherwise any emitted response declares
    `audit_suspended` and omits `receipt_id`; this rejection arm excludes
    `MUTATION_OUTCOME_UNKNOWN`, which is not a rejection;
23. journal-RW/index-RO accepts durably without claiming indexed delivery, while
    journal-RO keeps reads available and rejects every mutation;
24. native causes enforce foreign keys, typed external causes can cite bridge
    entries, and neither path elevates a bridge entry's authority;
25. after claim rollout the compatibility endpoints write leases only and a
    conflicting legacy claim cannot authorize a second owner;
26. every accepted transition records the measured build attestation without
    allowing request metadata to choose it;
27. F-10 is open and owned by M3/M4: a SIGKILL after an operational transaction
    rolls back but before its rejection is audited can leave that rejection
    without a record; if an external effect committed first, it can remain
    without the later audit record. Its positive control documents the window;
    its negative control scans every published surface and fails if the audit
    guarantee is stated without its conditions;
28. no R3 response, public `/health` payload or public `/ready` dependency
    distinguishes substrate states. Internal operator sensors retain that
    diagnosis.

## Consequences and non-goals

This creates two clocks—accepted coordination state and Markdown/search
visibility—but makes the distinction observable. It adds a small local session
protocol and outbox worker so that principal and runtime instance are not
collapsed into a client assertion. It deliberately does not add a workflow
engine, message broker, Kubernetes, vector database, chat UI or multi-node
consensus to v0.9.

It does not close the window between an effect and its denial/failure receipt.
That window is named as F-10 and owned by M3/M4. `audit_suspended` reports only
failures detected while a response can still be emitted; F-10 is not observable
inside the process that was killed.
