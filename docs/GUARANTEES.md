# The six guarantees — and what is true today

`llminbox` is a **coordination kernel for agent fleets**: it decides *who had the
authority to do what, in which lane, with what durable result, and how that is
demonstrated afterwards* — without asking you to abandon the markdown logs your
agents already write.

Six guarantees define that kernel. This page states each one, what it costs you if
it is missing, **its status in the code you are reading**, and the falsifier — the
observation that would prove the guarantee is not being met.

> **How to read status.** `IMPLEMENTED` means code and executable controls exist in
> this tree. `CERTIFIED IN THE v0.9 PILOT` additionally means the behavior was
> exercised against the deployed single-lane artefact built from `a0cfc2e` and
> recorded in [V0.9-CERTIFICATION.md](V0.9-CERTIFICATION.md). It does not extend the pilot's
> stated limits to HA, arbitrary external effects or fleet-scale load.

---

## Two modes, and every sentence on this page belongs to one of them

Almost every honest statement about this software is **true of one mode and false
of the other**, so the mode is stated rather than assumed. Reading a limit without
its mode is how a temporary state gets quoted back as the product's definition.

| | **Legacy** | **Bridge** | **Native-required** |
|---|---|---|---|
| **Where it applies** | Narrative/file workflows not admitted to native authority | Migration period with native sessions available and legacy compatibility retained | A lane whose operational admission barriers were opened by an operator |
| **How a write arrives** | Markdown append or legacy HTTP/CLI writer | Native session for authoritative operations; legacy appends remain compatible but unverified | Native API with a short-lived Bearer session; no authoritative local fallback |
| **Identity** | Header actor/lane are caller-declared labels | Native records are server-derived; legacy records remain labels | Principal, role, runtime instance, lane and capabilities are server-derived |
| **Authority record** | Markdown | Native journal for native operations; markdown is a projection/bridge | Native journal; markdown is a non-authoritative compatible projection |
| **Direct markdown append** | The legacy write | `legacy_unverified` for migrated operational semantics | Never grants authority for the admitted operational verbs |
| **Authentication** | Shared `X-Llminbox-Token` on legacy data routes | Legacy token for legacy routes; Bearer session for native routes | Bearer session and exact capabilities; operator changes admission separately |
| **Failure behavior** | The disposable index may be rebuilt; file tools continue | Each path follows its own authority rules | Fails closed; runtime loss never silently becomes a legacy authoritative write |

Three consequences worth stating plainly, because they are the ones most often
quoted out of context:

- **The legacy bridge is not a deprecation.** It stays. A publication path that
  depends on a container being alive is abandoned the first day it is not, and
  then everyone goes back to an unvalidated shell append — which is worse than the
  bridge, not better. What migrates is **authority over specific verbs**, never the
  corpus.
- **"Markdown is the source of truth" is a statement about the legacy bridge.**
  For a graduated verb it is no longer true, and saying it there would be the
  dishonest half of the sentence.
- **Admission is lane-scoped and operator-controlled.** In the v0.9 operational
  contract `events.accept` and `outbox.requeue` move as one CAS-protected pair so a
  lane cannot be left half-open. `close` is reversible before sealing; `sealed` is
  terminal. Returning to the base service therefore requires the certified
  close/drain/seal/certify procedure, not a client-side mode toggle.

---

## G1 — Identity is not self-declared

**The promise.** The principal, role, runtime instance and lane of an operational
write are derived by the server from a validated credential, never from a name the
writer typed into a header or a body.

**Why it matters.** An entry whose author is a string is an entry any process can
forge. Once a second agent *acts* on what the first one wrote, a forgeable author
is a forgeable instruction.

**Status: IMPLEMENTED; identity/admission exercised in the v0.9 pilot.** The native
session derives principal, role, runtime instance, lane and exact capabilities.
`/native/v1/whoami` reports that server-side identity; payload/header attempts to
self-declare it do not replace the session identity. Legacy header actors remain
labels and are not upgraded by this guarantee.

**Falsifier.** A credential for role A writes an entry declaring role B. If the
record shows B, G1 is not met.

---

## G2 — Authority is gradable, not all-or-nothing

**The promise.** An operator moves a lane's operational barriers to native authority
as an atomic, fenced pair. There is no global switch, and a client cannot promote
its own writes to authoritative.

**Why it matters.** A migration that demands the whole fleet move at once does not
happen; it gets reverted on the first day something breaks. Graduating one verb in
one lane is a change you can undo before lunch.

**Status: IMPLEMENTED and CERTIFIED IN THE v0.9 PILOT.** The pilot opened
`events.accept` and `outbox.requeue` together at epoch 1, closed them at epoch 2,
sealed them at epoch 3, and produced an atomic rollback certificate with zero
pending, failed or unresolved work.

**Falsifier.** With one verb graduated in one lane, an append that bypasses the
service still counts as authoritative for that verb. If it does, the graduation did
not happen — it was only documented.

---

## G3 — Delivery is demonstrable

**The promise.** An accepted event gets an identity, a causal position, an
idempotency key and a **durable receipt you can cite later**. The wire exposes the
actual lifecycle states rather than a synthetic “done”: `accepted`, `materialized`,
`indexed`, `delivered`, `delivery_progress`, `materialization_failed`,
`materialization_exhausted` and `materialization_repaired`. Native acknowledgement
is reported as `delivery_progress`, `delivered` or `no_ack_expected`.

**Why it matters.** "It was sent" is not "it arrived", and neither is "it was acted
on". Most coordination failures live in the gap between those sentences.

**Status: IMPLEMENTED and CERTIFIED IN THE v0.9 PILOT.** Exact replay returned the
same event and receipt identifiers and left one journal entry. See the concrete IDs
and commands in [V0.9-CERTIFICATION.md](V0.9-CERTIFICATION.md).

**Falsifier.** Same key and same body must return the original receipt with no
second mutation; same key and a *different* body must be rejected with no mutation
at all. If either mutates twice, G3 is not met.

⚠️ **What this guarantee is not.** It is **exactly-once admission** for a given key
and body — nothing more. Markdown projection, any observability export and every
external effect are **at-least-once**, so consumers must be idempotent. This
project does not promise end-to-end exactly-once delivery or exactly-once effects,
and you should distrust any coordination system that does.

---

## G4 — A job has exactly one live owner

**The promise.** Ownership is a lease with an expiry and a monotonic fence. A
previous owner cannot write after a takeover, even if it wakes up believing it
still holds the job.

**Why it matters.** Two agents that both believe they own a task do not collide
loudly. They each do half of it.

**Status: IMPLEMENTED.** Native claims use expiring leases and monotonically
increasing fences; stale-fence writes are rejected. This has executable API/CLI
controls, but the certification does not claim a 16-agent live contention run.

**Falsifier.** After a takeover, a write carrying the stale fence must be rejected.
If it lands, the lease is decoration.

---

## G5 — Recovery is unambiguous

**The promise.** Retry and replay do not create a second admitted domain mutation,
and a delayed command cannot revive a revision superseded by a newer order. External
effects remain at-least-once and consumers must be idempotent.

**Why it matters.** The dangerous crash is not the one that loses work. It is the
one that leaves work half-committed, so the retry does it twice.

**Status: IMPLEMENTED.** Journal mutation, receipt and outbox work share the domain
transaction; recovery drains durable pending work, and commands carry causality,
revision and supersession. The v0.9 pilot certified an empty rollback barrier after
exact event replay; it did not certify arbitrary third-party side effects.

**Falsifier.** Kill the process between the durable write and its projection, then
restart: the entry must exist exactly once and the pending work must still be
drainable. Duplicate or vanish, and G5 is not met.

---

## G6 — Reading at scale, without turning search into a boundary

**The promise.** Full-text search with exact filters, keyset pagination, snippets
and **explicit truncation** — a result that was cut says so in the response body,
not in the documentation. Lane and ledger filtering stays outside the text query.

**Why it matters.** A truncated answer that looks complete is worse than an error.
And a text query used as an access boundary is not a boundary at all: it is a
string comparison someone will eventually escape.

**Status: IMPLEMENTED.** Native search uses FTS5 with exact structured filters,
keyset pagination, snippets and explicit truncation. ACL/lane filtering is applied
outside the full-text expression; search text is never an authorization boundary.

**Falsifier.** A query whose result exceeds one page must say so in the response.
If the caller cannot tell a complete answer from a cut one, G6 is not met.

---

## Legacy bridge properties that remain shipped

These are the properties of the base you can run right now, each with a test in
`tests/`:

| Property | What it means |
|---|---|
| **The legacy index is disposable** | Delete that index and it rebuilds from markdown. `tail`, `grep` and legacy `>>` keep working. This does not apply to the authoritative native journal. |
| **Per-agent inbox and cursor** | "What is new for me" is answered by what was addressed to you since you last looked, not by the last N lines of everything. |
| **Identity resolution fails closed** | A name that is not in the census is rejected rather than silently given a cursor of its own. |
| **A validating writer that works with the service down** | It checks the census, that every recipient resolves, that the type is declared, and that neither headline nor body can open a second entry header. |
| **Self-healing index** | A corrupt index is thrown away and re-derived rather than filed as a broken ledger — and every rebuild stays visible afterwards. |

---

## Honest limits

The limits below are not caveats added for legal comfort. Each one is a decision
with a consequence you should weigh before adopting this.

- **This cannot close the shell-append door — and that is not the goal.** A process
  with write access to the file writes whatever it wants; this service lives outside
  it. What the kernel does instead, and what G2 is for, is make such a write
  **carry no authority** for a graduated verb. Anyone promising to prevent the
  append itself is promising something a sidecar cannot deliver.
- **`actor` is self-declared — in the legacy bridge.** Nothing verifies that an agent
  writing as a given name is that agent. In native mode the server derives the
  identity from the credential and the name in the header stops being the answer to
  "who wrote this". Both sentences are true; the mode is what distinguishes them.
- **One shared token, in the legacy bridge.** Every caller presents the same secret,
  so "which agent is asking" is not a question the service can answer, and per-agent
  authorization is absent rather than misconfigured. Native mode replaces this for
  graduated verbs; it does not retrofit the rest.
- **Lanes scope consumption, not access.** Any caller holding the token reads every
  mounted ledger through the legacy API. Native sessions bind lane and capabilities
  and native queries enforce them, but neither API isolates processes that already
  share the same host files or Docker authority. See [SECURITY.md](../SECURITY.md).
- **No non-repudiable end-user signatures.** Native frames use HMAC for symmetric
  integrity/authentication inside the trust domain, and native attribution is
  derived from the authenticated session. That does **not** prove a human or agent
  signed with a uniquely controlled private key, and it provides no non-repudiation:
  a holder of the relevant symmetric HMAC key can produce a valid frame. The earlier markdown
  hash-chain check was separately measured and retired because it was inert. These
  integrity and identity properties must not be advertised as end-user signatures.
- **No telemetry by default.** Nothing leaves the machine unless you configure an
  exporter and point it somewhere. The implemented OpenTelemetry-compatible export
  is opt-in, carries runtime lifecycle/coordination signals, and never carries
  prompt or tool content by default.
  Observability is an output; it is never the authority on what was admitted.
- **Single node.** The store is SQLite. That is a deliberate ceiling, not an
  oversight: it is revisited when a measured limit demands it — writers on
  different hosts, an availability target one node cannot meet, throughput outside
  a measured budget, or genuine geographic fan-out. "We have a lot of agents" is
  not one of those limits.
- **Append-only does not mean you cannot withdraw an entry.** See the section below;
  the earlier wording said this software makes selective erasure impossible, which
  was wrong in the direction that matters.

---

## Withdrawing an entry, and what "append-only" actually constrains

"Append-only" describes how this software *reads and indexes*, not a lock on your
files. Three different things are involved and they are usually collapsed into one:

**1 · The legacy source is yours.** Your ledger is a markdown file on your disk,
edited by the tools you already use. Removing an entry from it needs no feature
from this project, no migration and no permission — you edit the file. Nothing here
holds a second copy of the text that would survive that edit.

**2 · The index carries a derived tombstone.** When an entry stops being present in
the source, the index does **not** silently forget it: its row is marked absent with
the moment it disappeared, and `llmi verify` reports it. That is deliberate. A
coordination record that can lose entries without anyone noticing is worse than one
that cannot lose them at all, so removal is made **visible rather than prevented**.
The consequence to plan for: a legitimate withdrawal and an accidental loss look the
same to the integrity check until a human says which it was.

**3 · The native journal is the part that needs a policy, and does not have one
yet.** The journal is append-only *for authority* — that is what makes a receipt
mean anything a month later. That property is in tension with erasure, and the
tension is real rather than rhetorical: a record whose purpose is to prove what was
admitted cannot also promise to forget on request without saying how.

**Status: OPEN POLICY GAP.** The journal exists, but its retention and withdrawal
policy is not written. What a v1.0 has to
state, and what this page will carry when it does: how long journal entries are
kept, what a withdrawal writes into the journal (a supersession entry, not a hole),
what survives it — typically the fact that something was admitted and by whom, not
its content — and who can request one. **Until that policy exists, do not point this
at records you may be obliged to erase**, and treat the paragraph above as the
description of a gap rather than of a feature.

**Whatever the policy says, the legal basis and the data lifecycle remain the
deployer's.** This is a self-hosted index over your own files; there is no operator
of it but you.

## Build versus integrate

The fastest way to make this product worthless is to grow it into the things it
sits between. The line is drawn here, and it is meant to be quoted back at us:

### Built here — the kernel

Typed append-only journal with identity, causality and supersession · command and
mutation idempotency with a receipt bound to the same domain transaction · durable
outbox with bounded retries and a dead state · leases with expiry and monotonic
fencing · full-text search with exact filters, explicit truncation and keyset
pagination · fail-closed migrations with rollback evidence.

### Integrated — with a border that does not move

| Neighbour | Relationship |
|---|---|
| **Agent frameworks and runtimes** | Producers of lifecycle, job and tool events; consumers of commands. **Never** the source of principal, role or lane. |
| **Collaboration workspaces and chat UIs** | Destinations and origins of messages, and operator surfaces. Not internal databases of the kernel. |
| **Gateways and harnesses** | Authenticate the *instance* crossing the gateway. The kernel does not believe a name written in prose or in a payload. |
| **Wire protocols** (event envelopes, tool and agent-to-agent protocols) | Versioned adapters. Transport never decides identity or authority; every input is normalised to the native contract and every output declares its version and any loss of meaning. |
| **Observability collectors** | An export target. An exporter that falls over does not reverse a durable mutation — it only makes the degradation visible. |
| **Durable execution engines and message brokers** | Possible future backends *behind* the same contract, adopted only when a benchmark or a topology requires them. Their acknowledgement is a transport fact, never proof that a business effect happened once. |

### Explicitly not built

A new chat UI as a 1.0 requirement · a model runtime, prompt builder, cognitive
memory or agent marketplace · authority derived from a written name, a header, a
prompt or a trace parent · **any promise of exactly-once external effects** ·
an LLM or a log sentence used as the arbiter of health, authorisation or
completion · a vector database for a need that exact filters and full-text search
have not been measured against first · unbounded metric cardinality or prompt and
tool payloads in metrics by default · calling UI routing or query filters
"isolation".

---

*Implementation status is grounded in the executable tree; deployed claims are
limited to the evidence in `V0.9-CERTIFICATION.md`. If either is not true, that is a
bug in this document and should be reported like any other.*
