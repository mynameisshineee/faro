# Minimal example

Two agents, one ledger, eight entries. Enough to see what the inbox does and not
one entry more.

## Run it

```bash
cp examples/minimal/roster.example.json roster.json          # or merge it into your own
export LLMINBOX_LEDGERS=team=$PWD/examples/minimal/team-ledger.md
./llmi build && ./llmi up

./llmi inbox alice-backend        # what is addressed to her, since she last looked
./llmi peek  bob-reviewer         # the same view for him, without consuming it
```

## The comparison that is the whole product

```bash
./llmi inbox alice-backend
tail -40 examples/minimal/team-ledger.md
```

Both show you the same file. The first answers *what is new for me*; the second
answers *what happened recently to anyone*, and leaves you to filter by eye. At
eight entries the difference is a curiosity. At twenty long entries an hour it is
the reason this exists — see
[the honest threshold](../../README.md#when-does-this-actually-help-the-honest-threshold).

## Two things this example is deliberately showing you

- **`carol-infra` is not in the roster.** Entry 7 is addressed to her. Run
  `./llmi lint` and you will see an entry that was written and delivered to nobody:
  a mistyped recipient *looks* addressed and arrives nowhere. That failure is the
  reason identity resolution fails closed.
- **Entry 8 quotes a header inside its body.** It is stored as text, not parsed as
  a second entry. Quoting other people's headers is something everyone does and has
  to keep working; the validating writer refuses to *publish* one, which is a
  different thing from refusing to display it.
