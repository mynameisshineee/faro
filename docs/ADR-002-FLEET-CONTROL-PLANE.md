# ADR-002 — fleet status, responsibility and recovery

Status: **accepted for v1.0 beta implementation**

Date: 2026-09-07

Base: the certified v0.9 baseline — source commit
`a0cfc2e28cc895b4bb3a4a915521e16b9e4bdd08`, built as an OCI image whose
archive SHA-256 is
`66972a1e42eefedabbd481303b8a07ee5b7ca7821dd8bdaf423bbb8c27dfe165`
(see [V0.9-CERTIFICATION.md](V0.9-CERTIFICATION.md)).

This decision is the integration authority for fleet status, responsibility
and recovery in the v1.0 beta. Earlier internal review documents informed it;
they are not part of this distribution, and this ADR prevails over them.

## Decision 1 — detector state is not operational status

`observability.Estado` remains the ten-value M3 detector vocabulary:

```text
viva-con-progreso · viva-sin-obligacion · atascada · en-bucle · muda ·
sin-armar · inarmable · ilegible · indeterminado · sensor-mudo
```

It answers what the detector can conclude from heartbeat, durable progress and
sensor evidence. It does not gain `stopped` or `recovering`; those are lifecycle
facts, not detector classifications.

The fleet control plane has a separate closed `RuntimeStatus` vocabulary:

```text
absent · fresh · stale · degraded · stopped · recovering
```

- `absent`: an expected workload in the active organisation revision has no
  active runtime binding or has never been observed. It is never inferred from
  an unknown identifier.
- `fresh`: an authenticated external supervisor acknowledged the exact active
  runtime instance within the deadline and no degrading evidence dominates.
- `stale`: the last external acknowledgement exceeded the deadline. A timeout
  alone never means `stopped`.
- `degraded`: authenticated detector or resource evidence reports an unhealthy
  condition while the runtime is not known to have exited.
- `stopped`: an authenticated external lifecycle observation reports exit or
  process absence for the exact active runtime generation.
- `recovering`: a recovery command was accepted for the exact target and remains
  unresolved. It ends only with a typed external success/failure observation or
  a superseding runtime generation, never with agent prose.

Every status response may expose the underlying M3 `detector_state` separately.
No consumer may translate `muda` directly to `stopped` or use an organisational
role as proof of liveness.

## Decision 2 — the supervisor is an authenticated observer

The supervisor is outside the agent process and has its own principal, session
and credential generation. The narrow capabilities are:

```text
runtime.observe
runtime.recover
runtime.read
organization.read
organization.activate
```

`runtime.observe` does not imply `runtime.recover`; neither implies another
agent's domain capabilities. Identity, lane, trust and observation time are
derived by the gateway. They are rejected if supplied in the body, headers or
query. Server time is authoritative.

An observation names a target runtime instance in the route, but the kernel
resolves it under the observer's server-derived lane in the same SQL predicate.
A target in another lane is indistinguishable from an unknown target: same 404
code, body and observable receipt count.

The observation vocabulary is closed:

```text
cycle_ack · started · exited · resource_degraded · resource_recovered ·
recovery_succeeded · recovery_failed
```

Free text, terminal pixels and provider quota prose never enter the beta's
durable observation row. The authoritative record carries only a closed
`reason_code` and bounded typed resource measurements. An operator may retain
redacted diagnostics in a separate, non-authoritative evidence system, linked
by observation id; those bytes never select a state and are outside the v1.0
beta wire.

## Decision 3 — one durable authority

The integrated candidate stores runtime observations, current status, status
transitions, recovery commands and organisation revisions in
`coordination.sqlite`, under `coordination.Journal` transactions and its schema
versioning rules.

A separate SQLite file is acceptable only as a disposable domain spike. It is
not integrable because it creates non-atomic reads, a second migration boundary
and a second receipt vocabulary for the same principal. The v1 migration must:

1. advance `durable_v` with exact recognised table and index shapes;
2. migrate v0.9 atomically and reopen the new form;
3. fail closed on partial or future forms;
4. demonstrate concurrent writers with `BEGIN IMMEDIATE`;
5. preserve a certified pre-migration snapshot for binary rollback.

The v0.9 binary is allowed to reject the newer schema as too new. The rollback
test therefore restores the certified pre-migration snapshot after admission is
closed and the new journal is drained; it does not pretend an old binary can
mutate a new schema.

DB Migrations is the sole owner of `coordination.py`, the v7 DDL, the atomic
v6-to-v7 migration and the retained, digest-verified SQLite snapshot primitive.
Infra invokes and retains that exact primitive in rollout and rollback; it does
not invent a second backup format. Backend supplies the domain evaluator and
native gateway binding without editing the schema or opening another database.

Schema invariants are durable, not comments in adapters: supervisor sequence is
unique for the observer and target runtime generation; runtime bindings and
recovery targets reference their lane explicitly; organisation references use
composite foreign keys over `(lane, revision, role)`; and one active revision
per lane is enforced by a partial unique index, not an inline pseudo-constraint.

## Decision 4 — observations and transitions are not the same row

Every authenticated observation is durable and idempotent by
`(observer_principal, lane, verb, idempotency_key)` plus a canonical request
hash. A repeated key and identical bytes returns the same observation id and
result; changed bytes conflict with zero mutation. `supervisor_seq` is monotonic
per observer runtime generation, and an old sequence cannot regress status.

A status transition is created only when status or its authoritative cause
changes materially. The observation, projection update, transition and receipt
are committed atomically. An observation that confirms the same state remains
durable but does not fabricate another transition receipt.

Each transition has its own immutable `transition_id` and one receipt whose
subject is that id. The current projection includes at least:

```text
lane · target principal/role/runtime_instance/credential_generation · status ·
detector_state · status_seq · status_since · last_observed_at · cause_id ·
transition_id · receipt_id · organization_revision
```

Rebuilding the in-memory evaluator after restart must seed it from durable
observations. Restarting the gateway without a new observation cannot itself
create a transition.

## Decision 5 — native wire

The beta wire is additive under `/native/v1`:

```text
POST /runtimes/{runtime_instance}/observations
GET  /runtimes
GET  /runtimes/{runtime_instance}
POST /runtimes/{runtime_instance}/recoveries
GET  /organization
```

Mutation routes require `Idempotency-Key`. List and item reads are always
lane-scoped from the authenticated session. Recovery uses the existing command
lifecycle and fencing primitives where applicable, but has a distinct
`runtime.recover` capability. Acceptance moves the projection to `recovering`
in the same transaction; the runtime adapter executes outside the kernel and
reports a typed outcome later.

There is no automatic kill, session revocation, lease takeover or admission
closure in v1.0 beta. `stale`, `degraded` and `stopped` are evidence for an
operator decision, not hidden authority.

## Decision 6 — responsibility is a versioned, lane-confined graph

The authoritative organisation is an operator-owned snapshot with an immutable
revision per lane. Its minimum model is:

- roles in a lane and revision;
- at most one `reports_to` edge per role;
- zero or more reviewer roles;
- typed escalation routes to roles;
- layer and optional policy metadata;
- source digest, activation time and freshness/attestation state.

Activation validates the complete snapshot before changing the active revision:
all references exist in the same lane and revision, `reports_to` is acyclic,
self-links are rejected, the revision is monotonic and exactly one revision is
active. Organisation data never modifies principals, credential bindings,
capabilities, sessions or leases.

The beta has no agent-facing organisation mutation endpoint. Activation is an
operator operation using `organization.activate`; ingestion may be a private
mounted snapshot, but its parsed canonical form and revision become durable in
the kernel before it is served.

Legacy `GET /organigrama` remains available for compatibility and keeps its
existing freshness behaviour. It is advisory and must be labelled
`authority:false`. Native clients and the v1 console use `GET
/native/v1/organization`; they do not merge the legacy endpoint into an
authoritative view. This prevents two conflicting sources from appearing as one
truth.

## Decision 7 — isolation needs two different falsifiers

Static separation of Docker networks, volumes and environment references is a
necessary deployment precondition, not proof of kernel isolation. G9 requires:

1. a single-kernel adversarial fixture with two authenticated lanes and
   deliberately colliding logical names, which attempts cross-lane sessions,
   events, receipts, search cursors, observations and recoveries; and
2. a two-deployment topology with separate networks, volumes, credentials and
   attested secret fingerprints.

Different environment-variable names do not prove different secret values.
Fingerprint comparison uses a keyed or already attested identifier, never the
raw secret. A disabled search endpoint does not count as a successful cursor
falsifier, and volume initialisation does not count as workload recovery.

## Decision 8 — console truthfulness

The console is read-only in beta. It distinguishes loading, unavailable,
unobserved, stale, degraded, stopped, recovering and request failure. It shows
the status receipt and organisation revision used for every joined row.

Until the native endpoints exist, the UI may expose an unavailable adapter seam
or a separately labelled legacy view. Returning an empty array is not evidence
that the fleet has zero failures. Runtime schemas are validated at the boundary;
`unknown` is not accepted as a version or revision.

## Required falsifiers before integration

- every `RuntimeStatus` is reachable through a production path and a positive
  control;
- timeout reaches `stale`, never `stopped`;
- external exit reaches `stopped`; self-report cannot;
- recovery without `runtime.recover` is denied; escalation authority is
  insufficient;
- restart without new evidence writes zero transitions;
- two Journal instances cannot allocate the same sequence or receipt;
- observation replay and conflict preserve one result/zero extra mutation;
- cross-lane item reads are indistinguishable from absent and write zero rows;
- organisation cycles, unknown references and cross-lane edges fail atomically;
- same-role principals remain distinct and the graph grants zero capabilities;
- the legacy organisation endpoint cannot silently populate native authority;
- the sixteen-agent/two-lane test reports every truncated measurement and does
  not claim the million-entry envelope from a smaller corpus.

## Consequences

Backend may keep its current standalone liveness module as a spike while
adapting the domain model, but must not certify or integrate its persistence.
DB Migrations owns the durable schema change and rollback evidence. Infra owns
both isolation layers and the external runtime adapter. Frontend targets the
native read contracts and labels legacy data honestly. CTO, Contracts, Security,
QA and SDET review the exact resulting SHAs; no handoff self-certifies them.
