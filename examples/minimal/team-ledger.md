# team — coordination ledger (example)

Everything below is invented. See `README.md` in this directory.

### [alice-backend → bob-reviewer · PRODUCED] 2026-03-02T09:12:00Z — refunds endpoint ready for review
`POST /refunds` is behind the `refunds` flag, off by default. Idempotency key is the
provider's reference, so a retried webhook cannot refund twice.

Not covered: partial refunds. The provider models them as a separate object and I did
not want to guess the shape.

### [bob-reviewer → alice-backend · FINDING] 2026-03-02T11:40:00Z — the retry path can refund twice after a timeout
The key is read before the write and the window between them is not inside a
transaction. Two webhooks 40 ms apart both saw "not refunded yet".

Reproduced with two concurrent requests carrying the same reference: two rows.
Control: same test with a 2 s gap gives one row, so it is the window and not the key.

### [alice-backend → bob-reviewer · ACK] 2026-03-02T12:05:00Z — confirmed, moving the check inside the transaction
Your reproduction is the test now. It fails on the current code, which is what makes
it worth keeping.

### [release-bot → team · FYI] 2026-03-03T08:00:00Z — nightly build 412 green
Suite green, image published. No behaviour change: this is the same commit as 411 with
a rebuilt base image.

### [alice-backend → bob-reviewer · DELIVERED] 2026-03-03T14:22:00Z — refunds: single-row under concurrency
Check and write are one transaction. Bob's two-request reproduction now yields one row;
the 2 s-gap control still yields one. Flag stays off until the provider sandbox run.

### [bob-reviewer → team · MEASURED] 2026-03-04T10:05:00Z — review latency is not where the time goes
Nine of the last twelve changes waited longer for someone to notice they existed than
for the review itself. Median time to first look: 6 h 20 m. Median review once started:
18 m.

That is a reading problem, not a reviewing problem.

### [alice-backend → carol-infra · REQUEST] 2026-03-04T16:30:00Z — need the sandbox credentials rotated
This entry is addressed to a name that is not in the roster. It is written, it is
readable, and it is delivered to nobody. Run `llmi lint` to see it counted.

### [bob-reviewer → alice-backend · FYI] 2026-03-05T09:00:00Z — quoting a header, on purpose
Somebody asked what happens to an entry whose body contains something like
`### [someone-else → team · PRODUCED] 2026-01-01T00:00:00Z — not a real entry`.

It is stored and shown as text. Quoting other people's headers is normal and has to
keep working; what the validating writer refuses is *publishing* a body that opens
one, which is a different thing.
