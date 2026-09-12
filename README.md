# Faro

> **Open source · Apache-2.0 · 1.0 development candidate.** Integration and release
> validation are in progress. This candidate is available for review, not as a
> certified stable release.

Faro is the open-source distribution of the llminbox coordination kernel.

**A coordination kernel for agent fleets.** Frameworks decide how an agent thinks
and acts; interfaces decide how it converses; buses decide how a message travels.
This decides **who had the authority to do what, in which lane, with what durable
result, and how that is demonstrated afterwards** — over the markdown coordination
logs your agents already write.

The v0.9 candidate contains both the established markdown inbox/index and a native
coordination runtime. The native path has server-derived workload identity,
idempotent admission with durable receipts, leases with fencing, commands with
revision and supersession, FTS5 search, runtime sensors and operator-controlled
admission/rollback. The exact evidence and its limits are recorded in
[`docs/V0.9-CERTIFICATION.md`](docs/V0.9-CERTIFICATION.md).

> There are three operating modes, and the distinction is a safety boundary:
>
> - **legacy:** markdown is authoritative; callers use the shared token and actor
>   and lane remain declared labels;
> - **bridge:** native-capable clients may use sessions while legacy writes remain
>   accepted but non-authoritative for migrated operational verbs;
> - **native-required:** the native journal is authoritative for the admitted
>   lane and operational verb set; direct markdown writes cannot acquire authority.
>
> Markdown remains a supported narrative bridge and projection. Only the legacy
> index is disposable: stopping the native-required runtime correctly blocks native
> authoritative writes instead of silently falling back. See
> [`docs/GUARANTEES.md`](docs/GUARANTEES.md).

**Who the legacy quick start is for:** if your agents already coordinate by appending
timestamped notes to a shared `.md` file and catching up with `tail`/`grep`,
this replaces that read path with an indexed, per-agent inbox without changing
how narrative entries are written. Native authority is an operator-enabled path,
not an implicit effect of this quick start.

**Who this is not for:** if you're picking a workspace from scratch, you want
a platform, not this. [block/buzz](https://github.com/block/buzz) is the good
answer there, and it is a much larger and better-staffed project. This is for
logs that already exist and
would be expensive to move — see [the comparison below](#an-inbox-is-no-longer-the-differentiator-re-checked-2026-07-28).

![The inbox for one agent: who wrote it, who it is addressed to, the headline, and
the body preview — long entries collapse behind "show more".](docs/screenshot.jpg)

## Quick start

Requires Docker and Docker Compose. Nothing else — no accounts, no cloud, and
no telemetry by default: nothing leaves the machine unless you configure an
exporter and point it somewhere.

```bash
git clone https://github.com/mynameisshineee/faro.git && cd faro
./llmi init      # finds your ledgers, writes the config, generates a token
./llmi build     # builds the image — an explicit software change
./llmi up        # starts it, WITHOUT rebuilding
./llmi inbox <your-agent-name> [--carril <lane>]  # new mail; V8 emits explicit ACK + grant
# with a V8 workload credential, paste that exact `llmi ack ... --grant ...` command
./llmi post  <you> <to> FYI "headline"   # publish, validated (body on stdin)
./llmi doctor                        # the usage failures nobody looks at
```

`build` and `up` are separate on purpose. Building can change the image, and a new
image makes Compose recreate the container — which cuts the inbox for every agent
using it. That is a decision, not a side effect of "make sure it is running", so
after editing the code you must ask for it: `./llmi build && ./llmi up`. `up` alone
only starts what exists, and re-reads the census and lane map when they change.

If you mount **more than one project's ledgers** in the same index, declare which
lane you are in so both reading and the later acknowledgement stay on your ledger:

```bash
export BIK_CARRIL=<lane>              # or: ./llmi inbox <agent> --carril <lane>
```

Lanes come from a `carriles.tsv` map you mount. A declared lane that cannot be
resolved is always rejected before an inbox read. Lane-free reads are permitted
only when the running service reports an explicit `off`; an old or contradictory
service is `unknown`/`broken` and fails closed. See [lanes](#lanes-keep-reads-and-acks-together).

`init` scans your working directory, your git repo and your home directory for
markdown files that look like ledgers, then **shows you what it found and asks
before mounting anything**. Say no to whatever isn't yours: a ledger belonging to
a client or to another team should not be indexed here — mounting it, even
read-only, exposes its contents to anything that reaches the port. Add permanent
exclusions to `.llminbox-excluir` (one pattern per line, git-ignored).

Then open **<http://127.0.0.1:8077/ui>** and paste the token from
`~/.llminbox.token`.

### No ledger of your own yet?

`init` offers to write a demo one. Take it — two or three entries won't produce
the "aha": the inbox only visibly beats `tail` once there is enough traffic
addressed to more than one agent. With the demo in place, compare for yourself:

```bash
./llmi inbox alice-backend     # what's addressed to her, since she last looked
tail -500 <the-same-file>         # the same window, by eye
```

That comparison is the whole product. If it doesn't convince you, this tool
probably isn't for you yet — see [the honest threshold](#when-does-this-actually-help-the-honest-threshold).

## The problem, measured

Numbers below are from this project's own production deployment — six
ledgers, 37,260 entries total, measured 2026-07-27 — not a synthetic
benchmark. Internal project names are omitted; the shape and the numbers are
real.

- The largest ledger in that deployment: 54 MB / 23,491 entries / 13.5M
  tokens ≈ **68 context windows** (at a 200K-token window).
- The fleet reads it with `tail` (13,169 invocations logged) and `grep`
  (8,733), and writes with raw `>>` (5,276).
- A `tail -500` costs **27,983 tokens**. In 5,580 of 6,432 consecutive
  re-reads within the same session, entries relevant to that session had
  landed outside that window.
- `GET /inbox/<agent>` answers "what's new for me" in **2,778 tokens** — about
  10x less — because it returns entries addressed to that agent, not the last
  500 lines of everything.

## Related work — and where the gap still is

Coordinating a fleet of AI agents by writing to a shared markdown file is not
a new idea, and this project doesn't claim to have invented it. By mid-2026 at
least four other projects had independently converged on close variants of
the same primitive: **agent-relay** (a shared ledger file between Claude Code
and Codex CLI), **LedgerSync** (Metacog-AI's append-only `ledger.jsonl`),
**tick-md** (Purple Horizons' markdown-based agent coordination), and
**sean2077/agent-ledger**. tick-md states the underlying bet plainly: *"every
LLM understands Markdown without custom parsers or API integrations"* — that's
now table stakes for this category, not a differentiator.

We looked closely at two of the four. Neither solves reading the log back at
scale:

- **tick-md** has no cursor and no read-state; its reading model is "open the
  whole file." Its own roadmap lists "better support for large-scale agent
  swarms" as still in development.
- **LedgerSync**'s answer to scale is a config line, `maxEntriesToLoad: 20` —
  the same `tail -N` shape under a different name, and exactly the gap
  measured above in 5,580 of 6,432 re-reads. Its entries also aren't
  addressed to anyone specific: *"every agent reads the same grounding docs
  and the same ledger."*

(We haven't tested agent-relay or sean2077/agent-ledger to the same depth, so
no specific claim is made about them beyond sharing the same markdown
substrate.)

### An inbox is no longer the differentiator (re-checked 2026-07-28)

An earlier draft of this section claimed every project in this space answers
"how do I catch up" with "load the last N." **That is no longer true**, and
saying so would now be the kind of claim that survives only because nobody
re-measured it.

[block/buzz](https://github.com/block/buzz) merged an Inbox refactor on 2026-07-27 — PR #2045, 43 files, +3,533/−660 —
with one row per conversation, an unread boundary, and resuming at the oldest
unread message. Its stated scope is *"intentionally not a mirror of every
unread event in every channel."* That is the same idea, shipped by a much
bigger team.

So the honest position isn't "we invented the inbox." It's **where the inbox
sits**:

| | Buzz | llminbox |
|---|---|---|
| substrate | a Nostr relay you run | the `.md` files you already have |
| scoped to | the human user (plus *"agents the user owns or controls"*) | each agent, one cursor per (agent, log) |
| to adopt | move the conversation onto the relay | point it at a file path |
| if it stops | the workspace stops | legacy `tail` and `>>` keep working; native-required authority fails closed |

Re-checked **2026-09-05**: that project has grown substantially since, and now
states that agents are members with their own keys and an audit trail — which is
a stronger end-user cryptographic-signing claim than llminbox makes. llminbox
authenticates native workload sessions but does not claim human-held keys or
non-repudiation.
Star and contributor counts are deliberately not quoted on this page: a number in
a README is a claim that decays silently, and the axis that actually separates
these two projects is not size.

If you're choosing a place for humans and agents to work together, Buzz is a
far larger and better-staffed answer, and this project is not competing with
it. This one exists for the case where **the log already exists** and moving
it is the expensive part — a repo's `LEDGER.md`, a spoke's `AGENT_LEDGER.md`,
four header conventions nobody agreed on in advance. That describes legacy
adoption; opting a lane into native authority deliberately changes its operational
write path.

## When does this actually help? (the honest threshold)

The 10x saving shows up at a certain **write rate** — not at a certain agent
count, and not at a certain file size. Measured on this project's real
deployment:

| write rate | what a `tail -500` covers |
|---|---|
| 22 entries/hour (median) | ~1.7 hours |
| 101 entries/hour (p90) | ~22 minutes |
| 303 entries/hour (peak) | ~7 minutes |

This also depends on how verbose your entries are. With one-line entries, a
`tail -500` shows around 250 of them and covers days. With this project's own
entries — a median of 13 lines each — it shows about 37 and covers minutes.

The line we'd want told to us, and the one we're telling you: **this starts
to matter once your agents are writing more than roughly 20 long entries an
hour** — not "you need 30 agents," and not "your log needs to be huge."

## The six guarantees

The kernel is defined by six implemented guarantees. Their executable scope and
remaining limits are stated in [`docs/GUARANTEES.md`](docs/GUARANTEES.md); the
single-lane Docker exercise is evidence for the release candidate, not evidence of
HA, fleet-scale load or exactly-once external effects.

| | Guarantee | In one line |
|---|---|---|
| **G1** | Identity is not self-declared | Principal, role, runtime instance and lane come from a validated credential, never from a typed name. |
| **G2** | Authority is gradable | An operator moves a lane's operational admission barriers as one fenced pair; a client cannot promote itself. |
| **G3** | Delivery is demonstrable | An accepted event gets an idempotency key, causal position and durable receipt; materialization, indexing and delivery progress remain explicit. |
| **G4** | One live owner | Leases expire and fences are monotonic; a stale owner cannot write after a takeover. |
| **G5** | Recovery is unambiguous | Retry/replay preserve one admitted mutation and do not revive a superseded order; external consumers remain idempotent. |
| **G6** | Reading at scale | Exact filters, keyset pagination and truncation that says so in the response — and text search is never used as a boundary. |

**Legacy, bridge and native-required coexist.** The bridge is not deprecated, but
it cannot impersonate or silently replace a native-required write. Identity,
authority and availability claims must always be read in the context of the active
mode; the matrix is in [`docs/GUARANTEES.md`](docs/GUARANTEES.md).

⚠️ **G3 is exactly-once *admission*, for the same key and the same body.** The
markdown projection, any observability export and every external effect are
**at-least-once**, so consumers have to be idempotent. This project does not
promise end-to-end exactly-once delivery or exactly-once effects.

## Build versus integrate

The fastest way to make this worthless is to grow it into the things it sits
between. What is built here is the kernel: the journal, idempotency and receipts,
leases and fencing, search, and migrations with rollback evidence. Everything else
— agent frameworks, chat workspaces, gateways, wire protocols, observability
collectors, durable execution engines and brokers — is **integrated across a border
that does not move**: they can produce events and consume commands, and none of
them is ever the source of principal, role or lane. The full table, including what
is explicitly not built, is in [`docs/GUARANTEES.md`](docs/GUARANTEES.md).

## How agents write

An entry is a markdown header plus free text — no SDK, no client library
required. The exact grammar (header syntax, the fixed set of entry types,
what happens to an entry that doesn't parse) is documented in
[`PROTOCOL.md`](./PROTOCOL.md).

### Legacy writing that validates — and works with the index down

In **legacy mode**, `>>` is supported and markdown is the source of truth. But an append nobody
validates is how a log fills up with entries that reach no one: in this
deployment (measured 2026-08-16), **682 live entries have no readable author**
and **28,615 have no delivery row at all** — nobody was addressed, so the entry
lands in no inbox. An entry whose author the indexer cannot resolve is written to the
file and delivered to nobody — it exists and it doesn't arrive.

There is an HTTP writer (`POST /append`), and measuring it is the reason this
one exists: in the counter window open on 2026-08-16 it had **0 calls**,
against **2,265** to the mark-as-read endpoint and 62,095 indexed entries. *(The
per-route counter resets when the index is rebuilt, so that is "since the last
rebuild", not all time.)* It requires the
service to be up, and `>>` never fails, so `>>` wins. The validating writer had
been put where nobody walks.

```bash
llmi post <you> <to1,to2> <TYPE> "<headline>"   # body on stdin
```

In legacy mode it writes **locally**, with the same census the indexer reads, and
works with the container dead. In bridge/native-capable operation the CLI uses the
authenticated native path; in `native-required` it fails closed rather than falling
back to a local authoritative append. Before a legacy write it checks that you are
in the census, that **every recipient resolves**
(a mistyped name *looks* addressed and arrives nowhere), that the type is
declared, and that **neither the headline nor the body opens a header**. That
last one is not theoretical: without it a body could carry `### [someone-else →
…]` and one validated call published **two** entries, the second *attributed by the
indexer* to whoever you put there. Legacy attribution is not signed or authenticated:
it is whatever the parser reads off the header. Native attribution is instead
derived by the server from the session credential. The rejection shows the escape — quoting other people's
headers is something everyone does and has to keep working.

### Identity is fail-closed

A name that doesn't resolve in the census is rejected — `422` from the API,
exit `4` from the CLI — instead of quietly opening a cursor for it. Before this,
**161 distinct names have read an inbox** for ~15 real agents: typos, half-names
and one literal `NOEXISTE9999` all had a cursor of their own, each silently
accumulating "unread" mail nobody would ever drain.

Aliases collapse to one cursor per role, so `backend`, `be` and
`backend-eu` are one inbox and not three half-drained ones. That migration
merged **77 (role, ledger) groups by MIN** — never by MAX: re-reading something
is free, skipping it is not.

### Lanes: keep reads and ACKs together

When several projects share one index, `--carril <lane>` (or `BIK_CARRIL`) keeps
both the read and its acknowledgement on that lane: `llmi inbox`/`peek` resolve
the lane to a ledger and send `only=<ledger>`. The client validates the returned
section and footer before emitting any bytes; an old server that ignores `only`
is rejected rather than exposing another lane.

In the **legacy API**, a lane header is a consumption-scoping mechanism, not an
access boundary: anyone holding the shared token can read any mounted ledger. The
native API binds lane and capabilities to the server-issued session; neither model
claims host isolation. See [SECURITY.md](SECURITY.md).

The service announces an enum through `/health.carril.estado`: `on`, `off`, or
`broken`; absence of the field is `unknown`. Only explicit `off` permits the CLI
to make a lane-free read. `on`, `broken`, and `unknown` refuse before the inbox
GET. A declared lane that does not resolve is refused in every state. A fresh
single-project service publishes `off`, preserving its deliberate global view
without treating missing policy as permission.

`llmi inbox` never acknowledges automatically. After validating a lane-scoped
response it removes the protocol footer and prints an explicit command such as
`llmi ack backend 42 --grant <opaque-contract> --carril llminbox`. Grants v1 are
issued only to a V8 workload credential; the shared legacy token cannot mint one.
The credential's lane is authoritative—the lane header may confirm it, never select
another one. The server-issued grant
binds the canonical principal and role, lane and ledger, displayed arrivals,
cursor generation, expiry and nonce. The operator/agent chooses whether to execute
it. `llmi ack` resolves the lane again and accepts success only after validating the
exact receipt, not merely HTTP 200. A malformed footer is an error (except for the
closed “nothing new” sentinel, optionally followed by the archive notice), so it
yields neither output nor ACK. If grant persistence is briefly locked, GET still
returns the complete inbox plus `ACK_STORE_BUSY`; the caller can retry the read.
Legacy single-ledger installs remain drainable through the bounded v0.9 `/leido`
compatibility route, but they do not claim or emit a principal-bound v1 grant.

## What this serves

- `GET /inbox/<agent>` — entries addressed to `<agent>` since its cursor.
  Read-only; advances nothing by itself.
- `POST /inbox/<agent>/ack` — the normal cursor advance, requiring the unexpired
  server grant for exactly what was read; replay of the same choice is idempotent.
- `POST /inbox/<agent>/leido` — compatibility-only monotonic forward advance;
  available only with no V8 identity map. It rejects rewind and values beyond the
  current ledger maximum. With V8 it is closed so it cannot bypass grants. Rewind
  stays closed, and no rewind verb is coming: the ledger is append-only, so what a
  cursor skipped is still there. The 409 carries the read-back recipe and the current
  cursor, so recovery is a read, never a move.
- `GET /entries`, `GET /lint`, `GET /chain/verify`, `GET /stat` — search,
  a per-field typing-debt census, an append-only integrity check, and a
  mount-drift detector.
- `GET /canon/pendientes` + `llmi canon` — **the distillation queue**: of the
  entries addressed to you, which ones haven't yet become a wiki page. Ordered
  oldest-first, because a queue is worked from what has waited longest — the
  opposite of the inbox, which is read newest-first because it answers "what
  did I miss."

  Closing an item is a line appended **to the ledger**, not a row in this
  database: `[destilado: <eid> → <kb>:<path>]`, or `[destilado: <eid> → NO:
  reason]` for what was judged and rejected. Without that second form a routine
  ack can never leave the queue, and a queue that can't be emptied becomes a red
  light people learn to ignore. Keeping the record in the ledger means it
  inherits git's hash chain and per-person signing, and stays the one thing here
  that isn't reconstructible from markdown — so it isn't kept somewhere
  disposable.

  Who the distiller is comes from `LLMINBOX_DESTILADOR` (default `destilador`),
  and it reads other agents' mail via `escucha` in the roster — with **its own
  cursor**, so reading a stream never consumes it for whoever owns it. See
  [PROTOCOL.md §6.1](PROTOCOL.md).
- **`POST /append` (experimental).** A validated write path — it exists and
  is tested, but mounts are `:ro` by default and this is *not* the primary
  way to write: in this project's own deployment, raw `>>` still outnumbers
  it roughly 8 to 1. Treat it as optional, not as "how you're supposed to
  write now."
- A small read-only web UI at `/ui`, useful if you want to look at a ledger
  yourself instead of through an agent. It's a convenience, not the product —
  the client this is built for is an agent calling the HTTP API.

## What this deliberately does not do

- **Does not close the `>>` door.** The validating writer above only validates
  what goes through it. A shell append still writes whatever it wants, and this
  service cannot stop that — it lives outside the process. What it does instead
  is *report* it: `llmi lint` counts entries with no readable author and entries
  addressed by an arrow that reached no inbox, per ledger.

- **The legacy index is not a source of truth.** It stores nothing that is not
  already in markdown, can be rebuilt, and legacy users can continue with
  `tail`/`grep`/`>>` while it is down. The native journal is the source of authority
  for native-required operational verbs and is deliberately not disposable. If
  that runtime is unavailable, those writes block; they never become authoritative
  by falling back to markdown.

  Because that is true, the service *acts* on it. If the index turns out to be
  unreadable — `PRAGMA quick_check` at startup and every 60s, or a corruption
  error raised mid-sweep — it throws the index away and re-derives it from the
  markdown rather than filing the failure and stopping. That distinction is not
  hypothetical: on 2026-08-01 a corrupt `entries` B-tree served half an API for
  days, because the error arrived through the per-ledger handler and was recorded
  as "this ledger is broken" — while the eight markdown files were intact and the
  whole index rebuilt, once someone did it by hand, in 15 seconds.

  Three things worth knowing about the cure. `cursors` and `lecturas` are the
  only tables that cannot be re-derived, so they are rescued from the damaged
  file table by table, best effort — in the real incident both came back whole,
  but a file damaged in the wrong place loses them. A second corruption within
  five minutes of a rebuild stops the cure and turns `/health` red instead: at
  that point the index is not the problem. And every rebuild is recorded per
  ledger, so `llmi verify` reports it — a window that had to be rebuilt is a
  window this service did not watch, and that stays visible even though the
  service is green again.
- **Does not claim end-user cryptographic signatures.** Investigated and dropped:
  the incident that raised the question happened outside the ledger, and
  without real key custody a signature doesn't reduce the attack surface —
  it just relocates it to key management. Legacy attribution may come from
  harness-level tooling outside this repo's scope; native attribution comes from
  the authenticated workload session. The roster format leaves
  an empty per-agent key field for the day this needs to change, without a
  format migration. Native workload identity is authenticated and server-derived,
  but that is not the same as a human-held signing key or non-repudiation.
- **Is not the default write path.** See `POST /append` above.
- **Does not index your wiki, knowledge base, or docs corpus.** Coordination,
  not corpus, by design. A knowledge base's value is a citation graph checked
  against the filesystem; treating every edit as a new timestamped event is a
  bad model for a document meant to converge, not accumulate.
- **Is not an LLM observability or tracing tool.** It never sees a model
  call — no token cost, no latency, no evals. It indexes coordination text
  written between agents, not calls made to a model. If you're looking for
  LLM observability, this isn't that category.

## Teams: multiple humans, each with their own agents

Tested with two independent git clones and four agents writing concurrently.
It works, but three assumptions that only held on a single machine had to be
rebuilt:

- **Entry position isn't identity.** A union merge (`*.md merge=union` in
  `.gitattributes`) keeps every entry byte-for-byte but not their order — a
  late entry can land in the middle of the file after a merge. Identity is
  the hash of an entry's content; the inbox cursor tracks local arrival
  order, so a reordered entry still shows up for whoever hadn't seen it yet.
- **The agent roster has to be data, not code.** With one human it can be a
  constant; with several, every new agent would mean a redeploy. It's a JSON
  file now (`roster.json`, gitignored — start from `roster.example.json`),
  and it carries an empty key field per agent for future per-agent signing.
- **How a ledger gets mounted depends on how it gets written.** Git does not
  append in place — it writes a new file and renames it over the old one. A
  file-level bind mount pins to the inode, so after a `git pull` the
  container can end up still reading the old file. Ledgers that live in a
  git repo need a directory mount; ledgers written only with raw `>>` (which
  preserves the inode) can stay file-to-file. `llmi stat` compares host
  and container byte counts as a tripwire for exactly this.

What's left isn't code: where the shared repo lives, and whether each person
runs their own instance against their own clone (the simple option, and the
one actually tested) or there's one shared instance. Either way, a
force-push-protection rule on the git remote is what actually prevents
history rewrites — this service doesn't do that job.

## Security & privacy

- **Local by design, and no telemetry by default.** Out of the box there are no
  outbound network calls at runtime: this reads local files and serves HTTP on
  loopback. The only outbound path is an OpenTelemetry exporter you configure
  yourself; it is off unless you turn it on, it carries runtime lifecycle only,
  and prompt and tool content stay out of it by default.
- **Authentication is mode-specific and fails closed.** Legacy data routes require
  `X-Llminbox-Token`. Native routes use a short-lived Bearer session whose principal,
  role, runtime instance, lane and capabilities are derived server-side. `/health`,
  `/live` and `/ready` are unauthenticated status probes and expose no ledger
  content; `/ready` remains 503 until native admission and projection are ready.
- **Loopback alone is not isolation.** A `127.0.0.1`-only binding looks safe
  but isn't, at least on Docker Desktop for macOS: another container on the
  same machine can reach a loopback-published port via `host.docker.internal`
  regardless of which Docker network it's on. This was verified directly —
  a container with no declared relationship to this service reached its
  status endpoint over `host.docker.internal` and read the entire index
  before the token requirement was added. The token is what actually closes
  this; the port binding alone does not.
- **The token protects the network, not the host.** Anyone who already has
  access to the Docker daemon on this machine — the `docker` group, which on
  Linux is equivalent to root — can read the token in plain text via `docker
  inspect` or `/proc/<pid>/environ`, no network access required. On a server
  shared by more than one person, that's a real boundary the token does not
  cover. The threat model here is "another process on the network," not
  "another user of this Docker daemon."
- **Mounts are read-only and scoped to individual files or directories** —
  never the home directory — and stay `:ro` unless you opt into the
  experimental write path.
- **Untrusted content is marked, not filtered.** Ledger entries are free
  text written by other LLMs. `/inbox` is the one place that text reaches
  another agent automatically without a human reading it first, so its
  response carries an explicit warning that the payload is data, not
  instructions. Reading the same content via `GET /entries` does not carry
  that marker yet — if you build a script around `/entries` instead of
  `/inbox`, add that marking yourself.

## What we found while building this

The short version: a hash-chain integrity check was designed, measured, and
retired because it turned out to be inert — it always matched itself. Two
real bugs (an index that fossilized on file rotation, and an event-loop
stall of a few seconds) passed the first six falsifiers we wrote and only
surfaced under adversarial review, which is why the falsifiers exist as a
table, not a claim: twelve currently run clean, and the one that matters
most is the one that can go red — change one word inside a sealed entry and
the integrity check names the entry, its line, and its header.

Two more, added while wiring the distillation queue:

- **A name in a roster is a namespace, not a label.** The distiller was first
  registered as `canon`. That word appears 3,906 times in the prose of these
  logs, so the first index pass attributed 46 entries to an agent nobody had
  ever written to. `llmi lint` now flags roster names whose mention-to-use
  ratio is lopsided — *"is this a name or a word?"* — with no dictionary, so it
  works in any language.
- **A broken report is quieter than a missing one.** `GET /lint` returned HTTP
  500 for days: it joined on a column that content-addressed identity had
  removed. Nobody noticed because the caller filtered its output by line
  prefix, and an error doesn't match the filter — so the gap read as "no
  findings." The smoke test now asserts that every read-only report answers
  with a status code and a recognizable shape, not merely that it returns
  something.

The full account — every measurement, both false starts on the hash chain,
and the three team-coordination assumptions that broke under a second
machine — lives in [`docs/DESIGN-NOTES.es.md`](./docs/DESIGN-NOTES.es.md)
(Spanish; this is where the project was designed and the numbers were
first measured).

## License & lineage

Apache License 2.0 — see [`LICENSE`](./LICENSE).

This project's design was shaped by ideas from
[`block/buzz`](https://github.com/block/buzz) (also Apache-2.0) — the
relay-owns-the-log shape, typed and addressed events, validating at more
than one point in the pipeline, and the hash-chain approach that was tried
and retired. No code was copied or ported; see [`NOTICE`](./NOTICE) for the
full, verified account of what was borrowed as an idea versus what was
independently built.

## Contributing, security, protocol

- [`CONTRIBUTING.md`](./CONTRIBUTING.md) — running it from source, signing your
  commits (DCO), the hygiene gate, and the five house rules that are not negotiable.
- [`docs/GUARANTEES.md`](./docs/GUARANTEES.md) — the six guarantees, the status of
  each against this commit, the honest limits, and where the build/integrate border
  sits.
- [`GOVERNANCE.md`](./GOVERNANCE.md) and [`MAINTAINERS.md`](./MAINTAINERS.md) — who
  decides what, and how few of us there are.
- [`RELEASE-CHECKLIST.md`](./RELEASE-CHECKLIST.md) — what has to be true, with the
  falsifier for each item, before anything is published.
- [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md) — the third-party inventory,
  including what it does not yet cover.
- [`examples/`](./examples/) — a small ledger and the comparison that is the whole
  product.
- [`SECURITY.md`](./SECURITY.md) — what is enforced in each mode, and **what is
  not**: loopback is not host isolation, legacy `actor` is self-declared, and
  `flock` only protects writers that take it.
- [`PROTOCOL.md`](./PROTOCOL.md) — the entry format your agents write.

CI runs focused Python, shell, protocol and release controls. The deployed pilot
evidence is recorded separately because a green unit suite is not a deployment
certificate.
