## What this changes, and why

<!-- One paragraph. If it fixes a bug, say what the bug DID, not just that it existed. -->

## How you verified it

<!-- Commands and their output. "It works" is not verification.
     If you added a check, say what its falsifier is: what you would see if the
     property were broken. A check that cannot fail checks nothing. -->

- [ ] `IMAGEN=llminbox:test ./tests/humo.sh` passes
- [ ] `cd web && pnpm build` passes (if the UI changed)
- [ ] Colours, if touched, have their WCAG contrast **calculated** (not eyeballed)
- [ ] No new runtime network dependency
- [ ] Entry text is still rendered as text, never as HTML
