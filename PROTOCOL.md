# PROTOCOL — public contract 0.9.0

**Public protocol version: `0.9.0`.** This document specifies the legacy markdown
ledger grammar and its bridge semantics. The native wire is a separate, versioned
HTTP contract governed by
[`docs/ADR-001-NATIVE-AUTHORITY.md`](docs/ADR-001-NATIVE-AUTHORITY.md): its actor and
lane come from an authenticated Bearer session, not from the ledger header below.
Compatibility of this grammar does not grant a legacy append native authority.

This is the specification of what `ledger_parse.py` actually recognizes, derived
from reading the tokenizer and verifying it against a real multi-agent deployment
(six concurrently-written ledgers, 37,000+ real entries, several independent
agents and humans) — not from a wishlist of what the format was supposed to be.

If your agents are going to write into a ledger, this is what to write. If a
sentence here isn't backed by the tokenizer's actual regex or by something
measured against real entries, it doesn't belong in this document — file an
issue instead of trusting it.

## 1. What an entry looks like

An entry is a **header line** followed by a **body** (zero or more lines of free
text). An entry runs from its header up to — but not including — the next
header, or end of file:

```
### [asistente-backend → revisor · PRODUCED] 2026-07-27T10:15:00Z — pagos: endpoint de reembolsos listo para revisar
Contrato en `openapi.yaml`. Casos borde en el cuerpo del PR.
Sin cambios de esquema.
```

That whole block — header plus the two lines under it — is one entry. The next
line starting with a recognized header pattern (see §2) starts the next entry.
There is no explicit end-of-entry marker; the tokenizer infers it from where the
*next* entry begins. This means:

- A header line **must be the first thing on its line** (the tokenizer matches
  against the start of a line, `re.match`, not `re.search`) — `### [something]`
  mentioned mid-sentence inside a body does **not** start a new entry, because
  the match only fires at column zero. But every line of the file is checked
  this way, unconditionally — there's no awareness of fenced code blocks or
  quoting. If your body puts one of the §2 patterns at the **start of its own
  line** (say, showing a header example on its own line, or a quoted excerpt
  from elsewhere), it *will* be read as a new entry, even if that's not what
  you meant. Verified directly: a body line that opens with `### [esto de
  aqui]` on its own splits the entry there, full stop.
- A blank line before the header is common (for readability) but not required.
  The tokenizer doesn't care about blank lines; it only cares whether a line
  *starts* with a recognized pattern.
- The last entry in a file that's still being written can be mid-write (the
  writer hasn't finished the line yet). A reader that wants only *settled*
  entries should treat the last one specially; a reader that wants everything,
  including a possibly-incomplete tail, can read all of them. The tokenizer
  itself doesn't decide this for you — it hands back whatever text sits between
  one header and the next, complete or not.

## 2. Header conventions the tokenizer accepts

There isn't one header syntax — there are four, because the format was measured
against how agents actually write, not mandated up front. Measured across the
real deployment referenced above (≈37,100 entries):

| convention | share | example |
|---|---:|---|
| `### [...]` | ~94% | `### [asistente-backend → revisor · PRODUCED] 2026-07-27T10:15:00Z — ...` |
| `## <ISO>Z · a → b · TIPO` | ~1% | `## 2026-07-27T10:18:00Z · revisor → asistente-backend · FYI — ...` |
| `## [...]` / `## <date>` | ~5% | `## [investigador→revisor] nota (2026-07-27)` / `## 2026-05-24 — subject [FE→BE ask]` |

**`### [...]` is the one to write if you're starting from nothing.** The other
two exist because they showed up in the wild and the tokenizer was widened to
recognize them rather than declaring that traffic invalid after the fact — that
principle (recognize what's actually written, adjust the document, not the
other way around) is why this file gets updated when the corpus disagrees with
it, not the reverse.

All four are recognized by a single regex checked against the start of every
line (`H_ENTRY` in `ledger_parse.py`) — a line has to *start* with one of these
four shapes to be treated as a new entry.

## 3. Actor, recipients, type — how they're declared

Inside the header, whatever comes **before** the arrow (`→` or its ASCII form
`->`) is read for the **actor**; whatever comes **after** is read for
**recipients**. If there's no arrow at all, the whole header is searched once
for a single recognized name and that becomes the actor (no recipients).

```
### [asistente-backend → revisor ∧ investigador · PRODUCED] 2026-07-27T10:15:00Z — ...
       ^^^^^^^^^^^^^^^^    ^^^^^^^^^^^^^^^^^^^^^^^^
             actor                recipients (both — see §3.3 for the separator)
```

### 3.1 Actor

The actor is **the name closest to the arrow**, not the first name mentioned. A
header that name-drops someone earlier in a sentence before getting to the real
author (`asistente-backend cites @revisor's earlier note → revisor, investigador`)
attributes correctly to `asistente-backend` — the match nearest the arrow wins,
not the first one found.

Names are matched against `roster.json` (see §6) — **not inferred**. A name that
isn't in the roster doesn't match, full stop; there's no "looks like a name so
it's probably one" fallback (see §7 for why that's a deliberate, measured
choice). Matching is case-insensitive and canonicalizes to the roster's own
casing, so `ADA`, `Ada`, and `ada` all resolve to whatever casing the roster
stores.

**Co-authored entries** — two names joined directly, with only `+`, `/`, or `∧`
between them and nothing else, *right against the arrow* — are read as joint
authorship and the actor comes back as both names joined with `+`:

```
### [asistente-backend+asistente-frontend → equipo] 2026-07-27T10:17:00Z — deploy conjunto listo
```
→ `actor = "asistente-backend+asistente-frontend"`

This only fires when the pair sits close to the arrow (within the first ~40
characters of the bracket content). It's a narrow pattern match for the literal
`X+Y →` / `X/Y →` shape, not general multi-author parsing — a header that
mentions two agents' names somewhere in a long free-text headline, with an
unrelated arrow appearing later in that same prose, does **not** get this
treatment; the usual nearest-to-the-arrow rule applies there instead. See §7 for
why that boundary exists and what it protects against.

### 3.2 Recipients (`to`)

Everything after the arrow, up to the header's closing bracket (or an em-dash,
en-dash, or `] `, whichever comes first), is read for recipient names —
**every** name in that span, not just the first one. Parenthetical asides
explaining *why* someone's on the list are stripped before reading names, so
they don't swallow the rest of the list:

```
### [asistente-backend → revisor [motivo: le toca esta semana] ∧ investigador] ...
```
→ `to = ["revisor", "investigador"]` (the bracketed aside is discarded, not read
as a name)

### 3.3 Broadcast vs. named recipients (`to` vs. `difusion`)

`roster.json` has a `difusion` list — destinations that aren't a specific agent
(a whole-team address, e.g. `equipo`/`todos`). Names matching that list are kept
**separate** from `to`:

```
### [asistente-backend+asistente-frontend → equipo] 2026-07-27T10:17:00Z — deploy conjunto listo
```
→ `to = []`, `difusion = ["equipo"]`

This split matters because "nobody named in particular" and "the whole team,
deliberately" are different situations for anyone trying to measure how much of
a ledger is actually addressed to someone. An entry with only broadcast
recipients has `to = []` — it will **not** show up as "has a recipient" if you
only check `to`; check `to or difusion` if what you want is "was this addressed
to *anyone*, named or broadcast."

### 3.4 Type (`tipo`)

Two fields, and the difference matters:

| field | what it is | who sets it |
|---|---|---|
| `raw_tipo` | the **lexeme you wrote**, verbatim, taken from the type slot of the header | the tokenizer (`PARSER_V`) |
| `tipo` | the **canonical interpretation** of that lexeme, or `NULL` | the canon (`CANON_V`) |
| `canonical_kind` | Agent OS meaning, or `NULL`; never copied into legacy `tipo` | the explicit kind registry |
| `kind_registry_rev` | immutable registry revision used for this row, or `NULL` with `canonical_kind` | the explicit kind registry |

`raw_tipo` is evidence and is never destroyed. `tipo` is a small governed set
(§4) and is `NULL` whenever the lexeme isn't in it. A word that merely *appears*
in the headline is not a type:

```text
### [cto → qa · ACK tu INGESTED del jueves] …   raw_tipo=None   tipo=None
### [cto → qa · FINDING] …                      raw_tipo=FINDING   tipo=FINDING
### [cto → qa · MEDIDO] …                       raw_tipo=MEDIDO    tipo=MEASURED
### [cto → qa · CHISME] …                       raw_tipo=CHISME    tipo=None
### [HEARTBEAT asistente-backend] …             raw_tipo=HEARTBEAT tipo=None
```

Two sources, in this order:

1. **The type slot** — what sits after the last `·` in the bracket. This wins.
2. **The opening label** (§4) — only if there's no slot. `### [HEARTBEAT
   asistente-backend]` has no `·`, so the label is all there is, and that row
   above is the one case where `raw_tipo` doesn't come from a slot. Put both in
   and the slot still wins: `### [HEARTBEAT cto → qa · FYI]` reads `FYI`.

Either way, `tipo` is still `canonical_tipo(raw_tipo)` — and most opening labels
canonicalize to `NULL`, because only `RESP` and `ACK` are in the canon. That's why
`HEARTBEAT` has a `raw_tipo` and no `tipo`.

Until `CANON_V=1` the type was instead a **substring search over the header** in a fixed
priority order, which is why that first example used to resolve to `INGESTED`
even though the entry was an `ACK`. It doesn't any more: the type comes from the
slot, not from scanning prose. If you don't put a lexeme in the slot, you get
`raw_tipo = None` — the tokenizer never guesses.

Note what the `MEDIDO` row does **not** say: the header still reads `MEDIDO`.
Canonicalization governs interpretation, not what got written.

## 4. Types

```python
CANON_TIPOS = {"PRODUCED", "INGESTED", "FYI", "REQUEST", "ACK", "HELD", "AMEND", "DELTA",
               "MEASURED", "RESP", "FINDING", "RULING", "DELIVERED"}
ALIASES     = {"MEDIDO": "MEASURED"}
```

This is the **single legacy grammar authority**: `canonical_tipo()` decides what
`/entries?tipo=` will match. There is no second list for legacy `tipo`. The eight
in the first row are the original set; the four after them earned their place by
author-breadth in the corpus; `DELIVERED` is the operational delivery receipt; and
`MEDIDO` is the one alias with surviving evidence (14 of its 15 authors also write
`MEASURED`).

| type | means |
|---|---|
| `PRODUCED` | something new exists — a deliverable, a decision, a finding |
| `INGESTED` | you consumed something someone else produced |
| `FYI` | informational, no action implied |
| `REQUEST` | you need something from the recipient |
| `ACK` | acknowledging a `REQUEST` or a prior entry |
| `HELD` | blocked, waiting on something (person, gate, external event) |
| `AMEND` | correcting or updating a previous entry — the ledger is append-only, so a correction is a new entry, not an edit of the old one |
| `DELTA` | an incremental update to something already tracked |

Agent OS uses five explicit communication acts, but they do **not** enter this
legacy column. Their written lexeme is `raw_tipo`; their interpretation is stored
per row as `canonical_kind` plus `kind_registry_rev`, from the append-only registry
in `kind_registry.py`. They are not aliases of `REQUEST`, `HELD`, or `FYI`: aliases
would erase why the recipient is being interrupted. See
`docs/ADR-003-MESSAGE-POLICY-MIGRATION.md`.

There's also a second, smaller set of **opening labels** —
`HEARTBEAT`, `CARRY-FORWARD`, `CLAIM`, `CANON`, `CERT`, `DONE`, `RESP`, `AVISO`,
`MSG`, `ASK`, `ACK`, `STATUS`, `INFO`, `HANDOFF` — recognized only when they
open the header (right after `### [` or `## [`), before any actor name. These
exist mainly to keep `HEARTBEAT` from swallowing the actor slot (`### [HEARTBEAT
asistente-backend]` correctly reads `asistente-backend` as the actor and
`HEARTBEAT` as the type, not the other way around) and to give a type to
headers that don't put a canonical lexeme in the type slot at all.
They land in `raw_tipo`; most of them canonicalize to `tipo = NULL` (only `RESP` and
`ACK` are in the canon).

## 5. What happens when you don't declare something

**The tokenizer never guesses.** If a field can't be read — no recognized name
before the arrow, no type keyword anywhere, no arrow at all — the field comes
back `None` (or `[]` for recipients), and it's **counted**, not silently
dropped. Per-field coverage (`ts` / `actor` / `to` / `difusion` / `tipo`, as a
percentage of entries) is a first-class output of `ledger_parse.py` — run it
directly:

```bash
python3 ledger_parse.py <ledger.md> [more.md ...]
# or, to use the same ledgers the service is pointed at:
python3 ledger_parse.py    # reads LLMINBOX_LEDGERS, same format as docker-compose.yml
```

`llmi lint [ledger]` is the CLI surface for the same thing at the entry
level — it's meant to enumerate *which* entries are missing what, not just the
aggregate percentage, so the debt is actionable instead of just a number.

A concrete example, verified against the tokenizer: two names that **are**
individually registered (`revisor`, `investigador`) still resolve to
`actor = None` when someone hyphenates them into a compound that was never
registered *as its own name* — matching requires a whole token, not a fragment
of a hyphenated one, so neither piece matches on its own inside the compound:

```
### [revisor-investigador → asistente-backend] 2026-07-27T10:15:00Z — nombre compuesto no registrado
```
→ `actor = None` (not "the closest guess" — nothing, because nothing matched)

## 6. The roster (`roster.json`)

Names are resolved against `roster.json` (git-ignored — copy `roster.example.json`
to `roster.json` and put your own agents in it). It has three sections:

- **`agentes`** — the agents that can appear as `actor` or a recipient. Each has
  a `nombre` (matched case-insensitively) and a `humano` — the person
  accountable for what that agent writes. There's also a `clave` field, empty
  today; it's reserved for per-agent signing if the ledger ever needs to serve
  as evidence rather than just coordination — nothing verifies it yet (see §7).
- **`humanos`** — people who can also appear directly as actor/recipient (with
  optional case-variant `alias`es), for when a human posts directly instead of
  through an agent.
- **`difusion`** — broadcast destinations that aren't a specific agent (see
  §3.3).

### 6.1 `escucha` — reading someone else's stream without consuming it

An agent may carry an optional `escucha` list: other names whose mail also lands
in its inbox.

```json
{ "nombre": "canon", "humano": "alice", "clave": "",
  "escucha": ["wiki-vault"] }
```

This looks like an alias and isn't one. **The cursor still belongs to the
listening agent.** `canon` reads everything addressed to `wiki-vault`, and
marking it read moves `canon`'s cursor only — the session that actually answers
as `wiki-vault` still finds its own inbox untouched.

That distinction is the whole point. Without it, a second reader of the same
stream has to either share a cursor (the two consume each other's mail) or get
named explicitly in every entry — which means asking everyone else to change how
they write, the one thing this project promises not to do.

Nothing about it is specific to distilling: any second reader of a stream —
an auditor, a summarizer, a stand-in during someone's holiday — is the same
shape.

An **empty roster is a valid, deliberately-safe state**: with nothing to match
against, the name-matching regex is built to never match anything (rather than,
say, matching every word), so a fresh install with no roster configured
recognizes zero entries instead of hallucinating recipients out of common
words. You'll see every field come back empty until you populate it.

## 7. What this legacy format does NOT guarantee

Read this before treating anything above as stronger than it is. These are limits
of the markdown grammar, not denials that the separate native wire authenticates
workloads:

- **No signature, no identity verification.** `actor` is free text matched
  against a name list — nothing stops anyone from writing `>>
  ### [someone-else → ...]` and having it attributed to that name. The roster's
  `clave` field (§6) is where per-agent signing would eventually plug in; it's
  unused today. The append validator (`POST /append`, see §8) requires an
  `actor` field to be present, but doesn't verify it's the caller either — same
  trust model as raw `>>`, just with a required field instead of an optional
  convention.
- **Co-authored actor is one opaque string, not a list.** `"asistente-backend+asistente-frontend"`
  is a single value; querying "everything by `asistente-backend`" will not
  match it (see §3.1). If you need to query joint authorship reliably, don't
  rely on string matching against the combined form.
- **The `X+Y →` co-author pattern is narrow on purpose.** It only fires within
  a short span right before the arrow (§3.1). This was tuned against a
  concrete failure mode: long free-text headers (HEARTBEAT-style, hundreds of
  characters) sometimes contain an unrelated arrow deep in the prose, with two
  registered names happening to sit next to each other just before it by
  coincidence — reading those as joint authorship produced *worse* attributions
  than the plain nearest-match rule, not better. If your real use case is
  legitimate multi-actor headers that don't sit close to the arrow, this format
  doesn't support that today; it either misattributes to whichever name is
  closest, or (if you engineer around it) risks the same false-positive class
  the span limit exists to avoid.
- **Type extraction reads the slot, or the opening label if there is no slot —
  and nothing else** (§3.4). A lexeme sitting anywhere else in the header is not
  a type, including one you meant as the type.
  The trade is deliberate: the old substring search let a headline that merely
  *mentioned* another type steal the field, so it was removed in `CANON_V=1`.
  What you get instead is `raw_tipo = None`, which is honest but is still a
  header nobody can filter.
- **`difusion` is only as good as `roster.json`'s `difusion` list.** A
  broadcast term not listed there is read as if it were a specific named
  recipient — indistinguishable from a real agent from the tokenizer's point of
  view.
- **Entry identity is the SHA-256 of its full text**, not its position or
  sequence number. Two byte-identical entries collapse into one — this is a
  feature for surviving git merges (order isn't preserved across a merge, but
  content is; identity-by-content is what lets a mixed-in entry from a merge
  still be recognized as "new" to whoever hasn't seen it), but it does mean an
  accidental exact duplicate is silently deduplicated, not counted twice.
- **Concurrent writes via plain `>>` aren't locked.** Only the validated writer
  (§8) takes a file lock. Measured against the real corpus this hasn't produced
  observed corruption (zero interleaved headers found across tens of thousands
  of entries), but that's a measurement of what happened, not a guarantee about
  what could.

## 8. Appending legacy/narrative entries without breaking anything

This section specifies the **legacy markdown protocol**. For narrative records and
lanes that remain in legacy mode, the canonical way to write an entry is the shell
append operator:

```bash
printf '\n%s\n%s\n' "### [asistente-backend → revisor · PRODUCED] $(date -u +%Y-%m-%dT%H:%M:%SZ) — your headline" "your body text" >> path/to/ledger.md
```

- **Use `>>`, never `>`.** This is append-only by convention, not by
  filesystem enforcement — a `>` truncates the whole ledger. There is no
  recovery step described here for that; don't do it.
- **The markdown file is canon only for the legacy write.** The legacy index is
  derived and disposable and can be rebuilt from the file. For an
  operator-admitted `native-required` operational verb, the native journal is the
  authority and markdown is a compatible projection. A direct `>>` remains readable
  narrative data but cannot acquire native authority. If the native runtime is
  unavailable, the client must fail closed instead of silently treating this append
  as an authoritative fallback.
- **A leading blank line before the header is conventional, not required** — it
  just makes the file easier for a human to scan with `tail`. What actually
  matters is that the header text starts at the beginning of its line (§1).
- **One entry, one write, if you can.** The tokenizer cuts entries at header
  boundaries; splitting a single logical entry across two separate `>>` calls
  with time between them risks another writer's entry landing in the middle.

Native event envelopes, receipts, sessions, leases and commands are versioned HTTP
contracts rather than variants of this header grammar. Native identity comes from
the Bearer session, never from the `actor` text in this format. See
[`docs/GUARANTEES.md`](docs/GUARANTEES.md) for the mode matrix and
[`docs/V0.9-CERTIFICATION.md`](docs/V0.9-CERTIFICATION.md) for the exercised
single-lane contract.

## 9. Full worked example

Verified against `ledger_parse.py` directly (not hand-simulated) — this is
exactly what feeding this text into `parse()` returns:

**Input:**
```
### [asistente-backend → revisor ∧ investigador · PRODUCED] 2026-07-27T10:15:00Z — pagos: endpoint de reembolsos listo para revisar
Contrato en `openapi.yaml`. Casos borde en el cuerpo del PR.
Sin cambios de esquema.
```

**Extracted:**
```python
Entrada(
    ts       = "2026-07-27T10:15:00",
    actor    = "asistente-backend",
    to       = ["revisor", "investigador"],
    difusion = [],
    tipo     = "PRODUCED",
)
```

Copy that header shape, swap in your own actor/recipients/type/timestamp/body,
and it will parse the same way.
