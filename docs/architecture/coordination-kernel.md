# Architecture — the coordination kernel

> Minimal, versioned description of what this tree actually runs, at the
> commit this file ships in. It does **not** describe admission control,
> active-runner selection, or an operator console: none of that is wired
> into this tree, and this document says so explicitly rather than by
> omission. If a later commit integrates any of them, this file is stale
> until it is edited to say so — grep the falsifier in "What is NOT in this
> tree" before trusting either claim.

## What ships in this tree

The kernel that is live in `servicio.py` / `native_gateway.py`:

| Piece | What it does | Where |
|---|---|---|
| Native event contract | identity, causality, commands, receipts | `docs/ADR-001-NATIVE-AUTHORITY.md` |
| Journal / projector | markdown projection, crash recovery | `projector.py`, `projector_runner.py` |
| Search | indexed reads over the ledger | `search_store.py`, `search_cursor.py`, `search_contract.py` |
| Ledger protocol | the entry format every agent writes | `PROTOCOL.md` |
| Runtime boundary | storage/recovery split | `runtime_root.py` |

`docs/ADR-001-NATIVE-AUTHORITY.md` is the only accepted architecture
decision in this tree. Where this page and the ADR disagree, the ADR wins
and this page is stale.

## What is NOT in this tree

<!-- doctest:run -->
```bash
! grep -Eq '^  (admission|runner|operator):' docker-compose.pilot.yml &&
test "$(awk '/^services:/{f=1;next} f&&/^[a-z]/{exit} f&&/^  [A-Za-z0-9_-]+:/{n++} END{print n+0}' docker-compose.pilot.yml)" = 2
```

The pilot compose in this tree declares exactly two services — `gateway`
and `agente` (`docker-compose.pilot.yml`) — none named admission, runner
or operator. `llminbox-pilot-journal`, `llminbox-pilot-index` and
`llminbox-pilot-pepper` are **volumes**, not services; a flat `grep` for
indented keys returns all five and that is how the earlier count of four
was reached. The check above therefore asserts both halves: that the three
names are absent **and** that the service count is what this paragraph
says. An assertion of absence never verifies the claim of presence beside
it.
Work on admission control, active-runner selection and an operator console
exists elsewhere, against a different base; this document does not claim it
is merged here, because it is not.

`docs/M4-PILOT.md` says the same about the pilot as a whole, from the
infrastructure side:

> *"Este correctivo NO declara el piloto listo... el piloto exige además
> M1-M3 integrados y una corrida contra una instancia desplegada que aquí
> no se hace."* — `docs/M4-PILOT.md:305-307`

## Six guarantees — status here, not aspirational

The public OSS surface ([`GUARANTEES.md`](../GUARANTEES.md)) states six
guarantees. Two are worth flagging because their published text
and the measured state of this kernel disagree:

- **G1, "Identity is not self-declared"** — `SECURITY.md` ("What is NOT
  enforced") measures the opposite in this tree: `actor` is self-declared,
  and `roster.json`'s signature field (`clave`) is reserved and empty.
  Treat G1 as a target for the native event contract, not a shipped
  property, until that field is populated and checked.
- **G4, "A job has exactly one live owner"** — the published falsifier for
  G4 matches the substring `lease|fence`, which also matches a `.release()`
  call and a comment; it does not measure the fencing mechanism itself.
  Re-run any G4 claim with an identifier-scoped pattern (e.g.
  `fencing_token|lease_id`) before citing it as verified.

## Falsifiers this document stands on

<!-- doctest:run -->
```bash
python3 -c "import ast; ast.parse(open('native_gateway.py').read()); ast.parse(open('servicio.py').read())"
```

<!-- doctest:run -->
```bash
grep -q "Required falsifiers" docs/ADR-001-NATIVE-AUTHORITY.md
```

## Scope this document does not cover

- No test or gate was run beyond the two static checks above: no live
  service, no Docker, no exercise of the native event contract.
- The admission/runner/operator work was not read for content — only its
  absence from `docker-compose.pilot.yml` was checked. It may be correct,
  complete, and ready to merge; this document makes no claim either way.
- If `docker-compose.pilot.yml` gains an `admission`, `runner`, or
  `operator` service, the falsifier above turns red and this page needs a
  rewrite, not a footnote.
