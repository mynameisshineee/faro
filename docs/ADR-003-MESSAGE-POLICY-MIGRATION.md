# ADR-003 — Agent OS message kinds without rewriting legacy `tipo`

Status: accepted for the v1.0 beta client/ledger adapter. Scope: versioned
interpretation and client lint; this ADR does **not** claim server authorization.

## Problem

Fleet role-contracts allow ordinary communication only as `DECISION_REQUEST`,
`ESCALATION`, `CONSULT`, `HUMAN_INPUT_REQUEST`, or `CRITICAL_ALERT`. The v0.9
ledger's `tipo` is a different ontology: `REQUEST`, `ACK`, `INGESTED`, `FYI`,
`HELD`, `DELTA`, and later corpus-derived kinds. Mapping between them is lossy. A
`REQUEST` does not say whether an agent needs a decision, consultation, or human
input; `HELD` is not necessarily an escalation; `FYI` does not prove criticality.

The existing schema already reserves the correct boundary:

```
raw_tipo          exact lexeme written in the header
tipo              v0.9 legacy interpretation (CANON_V=1)
canonical_kind    Agent OS interpretation
kind_registry_rev exact immutable registry revision used for that row
```

## Decision

1. Keep `CANON_TIPOS` and `CANON_V=1` unchanged. The five Agent OS acts never
   appear in legacy `tipo` and are not aliases.
2. Introduce the explicit append-only registry `kind_registry.py`. Revision 1 is
   the five-act allow-list adjudicated in the shared role-contracts. A future
   revision may add lexemes, but cannot redefine one from an earlier revision; a
   semantic change requires a new `canonical_kind`.
3. Materialize each recognized row as `(canonical_kind, kind_registry_rev)`. New
   rows get their first matching revision when indexed. The explicit backfill only
   fills pairs where both fields are `NULL`; populated pairs are checked against
   their own revision and never upgraded silently. `raw_tipo`, `tipo`, Markdown and
   EID are untouched.
4. Let the legacy Markdown publisher preserve a registered Agent OS lexeme even
   though `canonical_tipo()` returns `None`. This is preservation plus versioned
   interpretation, not expansion of legacy grammar.
5. Add an opt-in guard to `llmi post`:

   - `LLMINBOX_MESSAGE_POLICY=off` (default) applies no role-policy enforcement.
     Acceptance of the five registered lexemes is an additive v1.0 extension, so
     output for those newly accepted inputs is intentionally not v0.9 output.
   - `advisory` reports policy/configuration errors but permits the post.
   - `enforce` rejects before network or filesystem mutation.
   - In bridge, the actor is canonicalized with the same `<alias>-<lane>` rule as
     the publisher and its role comes from `LLMINBOX_ROSTER`; typed `YO` is not the
     policy subject. In native, principal, role and lane come from authenticated
     `/whoami`. There is no `--role` override. Recipients resolve against the roster;
     a destination in `difusion` is a broadcast.
   - `communication.broadcast=false` rejects every diffusion destination in
     `enforce`; `true` permits it. Unresolved actors/recipients fail closed.
   - `LLMINBOX_ROLE_CONTRACTS=/path/to/role-contracts` selects `<role>.yaml`;
     `LLMINBOX_ROLE_CONTRACT=/exact/file.yaml` is the single-file option.

The contract reader accepts one exact generated subset: top-level `id`, exactly one
top-level `communication`, and exactly one two-space-indented `broadcast`,
`permitido` and `deprecado`. Booleans are bare `true|false`; lists are non-empty,
inline, unquoted tokens. Duplicate blocks/keys, tabs, nesting, multiline lists,
unknown communication keys and ambiguous indentation are rejected. This consumes
the 17 current generated contracts without adding a runtime YAML dependency.

## Compatibility and rollback

- Existing legacy producers keep working because `off` applies no role-policy
  enforcement; the v0.9 legacy canon and its semantic version do not move. The
  five registered Agent OS lexemes are an explicit additive input extension.
- A v0.9 reader routes the new entries by recipient, preserves the header and
  `raw_tipo`, and honestly reports `tipo=NULL`. It ignores the additive semantic
  columns.
- Returning to v1.0 deterministically reproduces the same first registry revision
  for every lexeme. A registry bump fills only previously unmaterialized kinds;
  already interpreted rows keep their own revision.
- There is no old-to-new translation. Producers migrate at the intent source;
  historical legacy entries remain historical.

Roll out `off` -> `advisory` with measured denials -> fix producers -> `enforce`
per fleet. The native DTO carries Agent OS acts as the explicit pair
`canonical_kind` + `kind_registry_rev`; the journal reproduces that pair against
the registry injected by the composition root and never feeds it to legacy
`canonical_tipo`. Projection writes the semantic lexeme to Markdown, where
`raw_tipo` preserves it, legacy `tipo` remains `NULL`, and the pair is materialized.

On a read-only legacy index, startup runs a pure audit: registry manifest, durable
revision+digest, every populated pair, and registered `NULL/NULL` rows. It never
writes or blocks the legacy inbox. `/health.message_kinds` reports `clean`,
`pending_materialization`, `stale_registry`, `unsealed`, `unavailable`, or
`corrupt`. It publishes runtime and actually sealed revision/digest separately;
an older valid seal is `stale_registry`, never current/clean. `corrupt` and
`stale_registry` close readiness while keeping legacy reads available.

`/entries` adapts its projection to the columns physically present. An unmodified
v0.9 index without `raw_tipo`, `canonical_kind` or `kind_registry_rev` returns the
legacy row with those fields `null` and
`kind_materialization_status=untrusted`; `/inbox` is unchanged. Asking that index
for `?raw_tipo=` returns the typed `503 semantic_filter_unavailable`, because an
empty result would falsely claim the absent evidence was searched. A registered
`NULL/NULL` row in a current sealed schema is explicitly
`kind_materialization_status=pending_materialization`.

Per-row `kind_registry_rev` is an exact SQLite `INTEGER`. Values merely coercible
to integers (including `REAL 1.5` and text `"1"`) are corruption; neither startup
path truncates or converts them.

Native automatic idempotency keys hash the canonical effective JSON payload. They
exclude typed `YO` (the server scopes by its authenticated principal/lane/verb) and
exclude the pre-normalized kind lexeme. Consequently the same server identity and
payload produces the same key across actor spelling or kind case; changing head or
body changes both payload and key. Bridge key behavior is unchanged.

## Security boundary

This is lint for an honest client. In bridge mode the canonical actor remains
self-declared input plus lane; in native mode `/whoami` derives identity from the
session. The guard cannot block a
direct append, cannot grant authority and cannot substantiate an isolation claim.
Authenticated server-side enforcement with audited denials remains a separate
control-plane requirement.

## Cheap falsifiers

- `tests/message-policy.sh`: actual `llmi post` wiring for off/advisory/enforce,
  allow/deny, malformed mode, broadcast false/true, server-derived native identity
  and the explicit semantic wire pair (with a fake transport; no network).
- `tests/message-policy-correctives.py`: immutable registry/digest, full-row startup
  audit, v1 -> v0.9 NULL row -> v1 rematerialization, canonical bridge actor and all
  five semantic acts accepted by the native journal, exact SQLite revision typing,
  and the synthetic runtime-r2/index-r1 stale-seal case.
- `tests/message-policy-service-compat.py`: file and directory read-only v0.9
  physical schema; health degradation, `/entries` compatibility/typed filtering,
  and the unchanged legacy inbox.
- `tests/pytest/test_docker_local_imports.py` and `tests/contexto-build.sh`: the
  runtime image/build context must contain `kind_registry.py`.
- `tests/pytest/test_message_policy.py`: complete legacy-canon freeze, registry
  revision/rollback materialization, Markdown/EID preservation, unknown and legacy
  rejection, recipient resolution, duplicate/nested contract rejection and all
  17 generated role-contracts.
