# Governance

This project is small and says so. Pretending to a committee structure it does not
have would be its own kind of dishonesty, so what follows is the arrangement that
actually operates, plus the conditions under which it changes.

## How decisions get made

**Maintainer-led, evidence-first.** The maintainers listed in
[`MAINTAINERS.md`](MAINTAINERS.md) decide what merges. There is no vote, because
there are not enough people for a vote to mean anything.

What replaces a vote is the house rule that a disagreement about behaviour is
settled by running the thing. A proposal that changes behaviour arrives with the
observation that would prove it wrong; a review that rejects it does the same. This
is not a stylistic preference — several of the bugs documented in this repository
survived review precisely because the check that was supposed to catch them could
not fail.

## Which changes need more than a merge

| Change | What it takes |
|---|---|
| Bug fix, docs, tests, refactor with tests green | One maintainer approval. |
| New behaviour on the public surface (HTTP contract, CLI flags, entry grammar) | An issue first, describing the behaviour and its falsifier. Contract changes are versioned; see [`PROTOCOL.md`](PROTOCOL.md). |
| Anything that weakens one of the six guarantees, or the honest limits alongside them | A written argument in [`docs/GUARANTEES.md`](docs/GUARANTEES.md) *and* an updated falsifier. A guarantee may be narrowed openly; it may not be quietly widened. |
| A new runtime dependency | Justification in the pull request. Every dependency is supply-chain surface, and this project has already had a build stopped by a transitive package published hours earlier. |
| Licence, trademark or ownership | Not a code decision. See [`RELEASE-CHECKLIST.md`](RELEASE-CHECKLIST.md); these are held outside the repository and are not settled in a pull request thread. |

## Becoming a maintainer

There is no application form. The path is a handful of merged pull requests, at
least one of which carried a falsifier that caught something, followed by an
invitation from an existing maintainer. Reviewing other people's changes counts as
much as writing your own; arguably more, since this project's failure mode has
consistently been a check nobody could fail rather than code nobody wrote.

## Stepping down, and what happens if nobody is around

A maintainer who stops having time says so and is moved to the alumni section of
[`MAINTAINERS.md`](MAINTAINERS.md). No ceremony, no hard feelings.

If every maintainer becomes unresponsive, this project is Apache-2.0 and the
markdown files it indexes are yours already: a fork loses nothing that matters, and
the index rebuilds itself from your own files in seconds. That is a deliberate
property of the design, and it is the honest answer to "what if you disappear".

## Code of conduct

[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) applies to every space this project
uses. Enforcement is a maintainer decision, and the escalation path is the one
described there.
