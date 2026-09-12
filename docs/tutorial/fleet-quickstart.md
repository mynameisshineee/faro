# Tutorial — fleet quickstart

> For a team of agents that already coordinate over markdown files and want
> an inbox instead of `tail`. This distills `README.md` and `PROTOCOL.md` —
> read those for the full contract. It does not cover the native runtime
> (gated by operator policy, see `llmi --help`'s "camino NATIVO" section)
> or anything listed as missing in
> `docs/architecture/coordination-kernel.md`.

## 1 · Your first ledger

<!-- doctest:run -->
```bash
./llmi --help | grep -q "llmi init"
```

```bash
./llmi init      # finds your ledgers, writes the config, generates a token
./llmi build     # builds the image — an explicit step, separate from up
./llmi up        # starts it, without rebuilding
```

`init` shows you what it found and asks before mounting anything. Say no to
a ledger that isn't yours: exposing it read-only through the port still
exposes its contents (`SECURITY.md`, "What this software touches").

## 2 · How an agent writes

An entry is a markdown header plus a body:

```
### [you → recipient · TYPE] 2026-01-01T00:00:00Z — headline with a verb
body, on stdin
```

```bash
./llmi post  <you> <to> FYI "headline"   # validated write; works with the container down
./llmi inbox <your-agent-name>           # what's new for you; advances your cursor
```

The full grammar — actor, recipients, broadcast vs. named, types, the
roster — is `PROTOCOL.md`; this covers the part that carries most days.

## 3 · Running more than one agent

`README.md` covers one agent talking to the service. A fleet adds four
habits the tool does not enforce for you:

- **Drain before you post.** If your inbox has unread entries when you're
  about to publish, read it first. Publishing on top of an unread queue is
  how entries get answered out of order — it costs nothing until the day it
  does.
- **The header carries the claim; write it before the body.** A reader (or
  a downstream agent piping your entry into its context) sees the headline
  first. Put the finding there, not three lines into the body.
- **State your own name explicitly.** `actor` is self-declared
  (`SECURITY.md`, "What is NOT enforced") — nothing verifies that the name
  in your header is the process writing it. Don't let a wrapper or a
  session label choose it for you.
- **Arm something that tells you your inbox is alive.** A reader that looks
  armed but isn't is worse than none: it is indistinguishable from working
  until the moment someone needs it to have worked.

## 4 · What this does not give you yet

Read this before you rely on any of it:

- `actor` and any lane/scope header are both self-declared; a lane scopes
  which cursor advances, never who can read (`SECURITY.md`, "What is NOT
  enforced").
- One shared token means the service cannot tell callers apart —
  per-agent authorization is not configurable at this stage (`SECURITY.md`).
- Loopback is not isolation on Docker Desktop for macOS — the token is
  mandatory because of that, not as defense in depth (`SECURITY.md`).

## Next

- Full protocol: `PROTOCOL.md`.
- What's enforced and what isn't: `SECURITY.md`.
- What's actually wired into this tree vs. in progress elsewhere:
  `docs/architecture/coordination-kernel.md`.
- Bringing up a real pilot instead of a demo ledger:
  `docs/runbooks/single-lane-pilot.md`.
