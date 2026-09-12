# Contributing

## Running it from source

```bash
./llmi init --demo    # creates a sample ledger so day one is not an empty screen
./llmi build          # explicit: `up` no longer builds
./llmi up
./llmi inbox alice-backend
```

`./llmi init` scans your disk for ledgers and **asks before mounting anything**. Say
no to whatever is not yours: mounting a ledger, even read-only, exposes its contents
to anything that reaches the port. Permanent exclusions go in `.llminbox-excluir`
(one pattern per line, git-ignored).

## Working on the UI

```bash
cd web && pnpm install && pnpm dev
```

Vite proxies the API to `127.0.0.1:8077`, so run the service first. The build is
compiled into the Docker image, so users never need Node — that is deliberate and
should stay true.

## Sign the CLA

The signing and verification workflow is still being prepared. External
contributions will not be merged until that workflow is available and the
required record has been checked. Do not put signatures, contact details or
identity documents in a public issue or pull request.

Before your first Contribution is merged you sign the [Contributor License
Agreement](CLA.md). It is signed once, not per commit, and it states the licence you grant
and what you declare when you contribute. Sign the **ICLA** if you contribute in your own
name; if your employer has rights in the work, your employer signs the **CCLA** as well.

The intended record contains your GitHub handle and the date; your name and
contact address are kept outside this repository. No automated CLA check is
currently active.

A pull request from someone who has not signed is not merged.

### About the DCO

Until 2026-09-11 this project certified provenance with a `Signed-off-by:` line under the
[Developer Certificate of Origin](DCO). That file stays in the repository because it is the
record of how earlier contributions were certified. **It is no longer the mechanism**: new
Contributions are covered by the CLA.

## Licence of contributions

Contributions are made under [Apache-2.0](LICENSE), the licence this project is
released under. This is not an extra condition we are adding: Apache-2.0 §5 already
says that any contribution you intentionally submit for inclusion is under the terms
of that licence unless you state otherwise. The additional contribution agreement
is described above and in [CLA.md](CLA.md). No copyright is assigned to anyone by
contributing.

If you cannot make that certification for some part of a change — vendored code, a
snippet with a different licence, anything you did not write — say so in the pull
request instead of signing off and hoping. That conversation is easy before a merge
and expensive afterwards.

## Before you open a pull request

```bash
docker build -t llminbox:test . && IMAGEN=llminbox:test ./tests/humo.sh
python3 tools/higiene.py
python3 tools/estado-garantias.py
```

The smoke test checks 13 properties. **Each one has its falsifier written next to
it** — what you would see if the property were broken. If you add a check, add its
falsifier too: a check that cannot fail checks nothing.

The hygiene gate is the second command, and it runs the same code locally that CI
runs — that is the whole point of it being a script rather than a block pasted into
a workflow. It looks for secrets, absolute paths from someone's machine, email
addresses, first-party domains, internal project names and documents that describe
an organisation rather than this product. It **never deletes anything**: it prints
what it found and the decision stays yours.

- Blocking findings (secrets, machine paths, addresses, first-party domains) fail
  the run.
- Warnings (internal names, internal documents) are reported with a count and do
  not fail it. That asymmetry is deliberate: a permanent red teaches people to
  ignore red, exactly as a permanent green teaches them to trust it.
- A finding that is genuinely fine goes in `tools/higiene-allowlist.txt` **with a
  written reason**. Entries without one are ignored, and every exception is printed
  on each run, because an exception nobody sees is indistinguishable from a hole.
- **Nothing is skipped in bulk.** Workflows and lockfiles are scanned like any other
  text, because a deployment credential and a registry URL are exactly what a
  whole-directory or whole-extension exclusion would hide — and a blind spot by
  construction shows up in no count at all.

The third command re-measures the status of each guarantee in
[`docs/GUARANTEES.md`](docs/GUARANTEES.md) against the code and fails **in both
directions**: if the page claims something the tree does not contain, and if the
page still calls a guarantee unbuilt after you built it. The second case is the one
that matters, because nobody notices it. If you implement part of a guarantee,
expect this to go red until you update the page — that is the gate working.

## Do not paste real coordination content into this repository

Issues, pull requests, tests and fixtures are public. Do not paste entries from a
real ledger into any of them — they routinely contain project names, infrastructure
detail and the names of people accountable for a decision.

Reproduce with invented names instead. `examples/minimal/` exists precisely so that
a bug report has something realistic to point at, and `./llmi init` will write you a
demo ledger if you would rather have one locally.

## House rules that are not negotiable

1. **Entry text is rendered as text, never as HTML.** It is written by language
   models. No `dangerouslySetInnerHTML`, no markdown rendering without a security
   review of the trust boundary.
2. **No network at runtime.** No CDN fonts, no remote images, no link previews.
   Everything is compiled into the image. Self-hosted means self-hosted.
3. **Contrast is calculated, not eyeballed.** Three tokens shipped failing WCAG AA
   because they looked fine. If you touch a colour, compute the luminance ratio.
4. **Dependencies are justified.** Each one is supply-chain surface — a build here
   already failed because pnpm rejected a transitive package published three hours
   earlier. That policy is a feature; do not relax it.
5. **The markdown files stay the source of truth.** The index is derived and
   disposable. If the service dies, nobody is blocked — that property is tested,
   and any change that breaks it is a change to what this product *is*.

## Style

Code comments are in Spanish — they encode measured lessons and the team reads
them. Everything user-facing is in English.
