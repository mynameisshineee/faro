# Runbook — single-lane pilot

> Scope: bring the pilot up for exactly one lane, to read one ledger through the
> gateway. This is not the fleet-wide, multi-lane deployment `SECURITY.md`
> measures against — it is the smallest configuration that exercises the real
> services instead of a mock.
>
> **This is a recipe, not a test report.** It was written from the source of the
> compose files, the preflight tool and the rollback script; no image was built,
> no container was started and no volume was touched to produce it. Everything
> below is pending execution on a candidate build — see
> [What this runbook does not certify](#what-this-runbook-does-not-certify).

## Services and setup commands

| | files | service | what it runs |
|---|---|---|---|
| quick start (legacy) | `docker-compose.yml` | `llminbox` | the legacy app, as `README.md` describes it |
| initialise secret volumes | `docker-compose.pilot-key-init.yml` | `pepper-init`, `projector-key-init` | copies supplied secret files into the pilot's named volumes |
| initialise volume witnesses | `docker-compose.pilot-estreno.yml` | `estreno` | prepares the journal and pilot-ledger witnesses |
| pilot | `docker-compose.pilot.yml` | `gateway` | preflight, then the legacy app under pilot config |
| pilot, native mode | `docker-compose.pilot.yml` **+** `docker-compose.pilot-native.yml` | `gateway` | preflight, then the native root (`--factory`) |

The quick start and the pilot declare **different services**, so they are never
combined: the pilot is `-f docker-compose.pilot.yml`, on its own, and native
mode is that file **plus** the overlay, in that order. The overlay needs no
change to the image's `CMD`: it replaces the `entrypoint`, and a compose
`entrypoint` overrides the image's `CMD` rather than adding to it.

The project name is `llminbox-pilot-m1`, so every named volume below is really
`llminbox-pilot-m1_<name>` as Docker sees it.

## Prepare the private files

Run the compose commands from the repository root. Create the private directory
before generating the secrets below; keep the files separate from the source:

```bash
mkdir -p ~/pilot-m1 && chmod 700 ~/pilot-m1
set -o noclobber            # `>` now refuses to overwrite: no silent truncation
umask 077
cat > ~/pilot-m1/pilot.env <<'EOF'
LLMINBOX_JOURNAL_VOLUME_ID=
LLMINBOX_LEDGER_PILOTO_ID=
LLMINBOX_JOURNAL_VOLUME_WITNESS=
LLMINBOX_LEDGER_PILOTO_WITNESS=
LLMINBOX_PILOT_CREDENCIALES_SHA=
LLMINBOX_PILOT_TOKEN=
LLMINBOX_PILOT_AGENTE_CREDENCIAL=
LLMINBOX_PILOT_LEDGER_HOST=
LLMINBOX_PILOT_LEDGER_TESTIGO_HOST=
LLMINBOX_PILOT_MAPA_HOST=
LLMINBOX_PILOT_ROSTER_HOST=
LLMINBOX_PILOT_PEPPER_HOST=
LLMINBOX_PILOT_PROJECTOR_FRAME_KEY_HOST=
LLMINBOX_PILOT_PORT=8099
EOF
```

Fill the paths, credentials and map digest from the inputs below. The first-run
steps generate the two identities and witnesses; their values go into this same
file. Compose refuses any required value that is still blank.

Use absolute host paths inside `pilot.env`. Keep `~` for shell commands such as
the `--env-file` argument below, rather than putting it in a host-path value.

**One trap in the steps below:** `--env-file` hands the variables to *compose*,
not to your shell, so a shell command in the same procedure — the `chown` of the
ledger bind — cannot read them. Export the two values it needs, explicitly, in
the shell you run these from, copying the exact values from `pilot.env`. The
file is the source of those values; a different shell value would change
ownership on a different directory. Do not `source` the file:

```bash
export LLMINBOX_PILOT_LEDGER_HOST=/srv/pilot/ledgers/llminbox
export LLMINBOX_PILOT_PEPPER_HOST=~/pilot-m1/journal.pepper
```

## What you must supply

Seven inputs. None of them is created for you, and the pilot will not invent a
default for any of them.

1. **A durable volume for the journal.** The preflight refuses to start when the
   journal path shares a device with `/` — a file on the container filesystem
   evaporates on the recreate that a deployment requires. The compose file
   already points `LLMINBOX_JOURNAL` at a named volume; what you supply is a
   deployment where that volume is real and durable.
2. **Two witnesses**, one for the journal volume and one for the pilot lane's
   ledger. You do not write them by hand: the first run mints them and prints
   them. See [First run](#first-run-estreno).
3. **A roster and a V8 credential map**, as host files mounted read-only
   (`LLMINBOX_PILOT_ROSTER_HOST`, `LLMINBOX_PILOT_MAPA_HOST`), plus the map's
   `sha256` in `LLMINBOX_PILOT_CREDENCIALES_SHA`. Startup compares the mounted
   map against that attestation and refuses when they differ, so the sha is not
   bookkeeping: it is the gate.

   The map is a JSON object keyed by the credential itself, and each entry takes
   **exactly four fields** — any other key is refused:

   ```json
   {
     "<credential>": {
       "principal": "pilot-projector",
       "rol": "projector",
       "carril": "llminbox",
       "capacidades": ["session", "outbox.project"]
     }
   }
   ```

   `capacidades` is a declared vocabulary, not a list of internal capability
   names: `session` is **mandatory** in every entry and grants nothing by itself,
   `outbox.project` grants `outbox_worker`, and `admission.operate` grants
   `admission_operator`. Two entries may not share a `principal`.
4. **Two host directories for ledgers**: the pilot lane's, mounted read-write
   (`LLMINBOX_PILOT_LEDGER_HOST`) — the only read-write ledger in the whole
   deployment — and a second one mounted read-only
   (`LLMINBOX_PILOT_LEDGER_TESTIGO_HOST`) as the negative control. The second is
   not filler: without it, the cross-lane refusal has nothing to run against and
   its green would be signed by the missing mount, not by the authorisation.
   Each ledger directory must contain a real, regular `LEDGER.md` on the same
   device as the directory itself; the first run refuses otherwise.
5. **Two secret files on the host, mode exactly `0600`**: the journal pepper
   and, for native mode, the projector frame key. Both have a size floor that is
   only enforced at startup, so get them right before you deploy:

   | secret | floor | mode |
   |---|---|---|
   | pepper | **32 bytes after `strip()`** | `0600` on the host |
   | projector frame key | 32 bytes | **exactly `0600`**, re-checked in the container |

   The pepper's floor is worth reading twice: the file is first read with a
   1-byte minimum and only then measured against 32, so a 20-byte pepper passes
   the first check and dies on the second — in runtime, with the window already
   spent. Whitespace does not count, and neither does anything above 4096 bytes:
   that is the ceiling for both.

   Both files are also checked for **ownership and exposure** inside the
   container: the reader refuses a file whose owner is not the reading process's
   **effective uid**, and refuses any file carrying a single group or other
   permission bit. That is what the init service's `CHOWN` is for — and it means
   `LLMINBOX_SECRET_UID:LLMINBOX_SECRET_GID` must be the uid the gateway actually
   runs as (`1000:1000` in the compose file as shipped). Set one and not the
   other and startup fails; loudly, but at three in the morning.

   The reader is also TOCTOU-safe, which is worth knowing before you decide how
   carefully to stage these files: it re-`fstat`s after reading and compares
   device, inode, size and both timestamps against the values it checked
   permissions on, and rejects a short read, so the file cannot be swapped
   underneath between the permission check and the read.

   **Generate them from a random source — a length floor is not a secret.**
   Thirty-two bytes of `aaaa…` pass every check above. What the floor counts is
   **bytes in the file** after whitespace is stripped, not bits of entropy, so a
   short random value padded to length would pass too. Give it both:

   ```bash
   umask 077                     # and still under `set -o noclobber`
   head -c 32 /dev/urandom | base64 > ~/pilot-m1/journal.pepper   # 44 bytes on disk, 32 of entropy
   head -c 32 /dev/urandom | base64 > ~/pilot-m1/frame.key
   head -c 32 /dev/urandom | base64                               # the pilot token, straight into pilot.env
   chmod 0600 ~/pilot-m1/journal.pepper ~/pilot-m1/frame.key
   ```

   Write them where the env file lives, never inside the repository.

   `base64` is what makes both readings true at once: 32 random bytes become 44
   characters on disk (45 with the newline, which `strip()` removes), so the file
   clears a floor that counts bytes while the secret carries the entropy the
   floor was meant to stand for.

   **The secret is the base64 text, not the 32 bytes behind it.** Decoding it
   "to be tidy" still clears the floor and yields a *different* pepper — and
   changing a journal's pepper is a migration, not a cleanup.
6. **A pilot token of its own** (`LLMINBOX_PILOT_TOKEN`), different from the
   fleet's. The fleet token would otherwise be inherited from the ambient
   environment and the pilot would be reachable with fleet credentials.

   Also supply `LLMINBOX_PILOT_AGENTE_CREDENCIAL`, the pilot agent's separate
   credential. The `agente` service is included in `up`; its required variable
   must be present even when you are concentrating on the gateway. This service
   currently waits after starting; it does not exercise an agent workload.
7. **Native mode only — a projector principal in the V8 map.** With
   `LLMINBOX_PROJECTOR_MODE=active` the map must contain **exactly one**
   credential whose `principal` equals `LLMINBOX_PROJECTOR_PRINCIPAL_ID` (the
   overlay's default is `pilot-projector`), and the capabilities it grants must
   be **exactly** `outbox_worker` — not a superset. In the file's own vocabulary
   that is `"capacidades": ["session", "outbox.project"]` and nothing else: add
   one more verb and startup refuses. Two matches, zero matches, or one extra
   capability each raise. The projector's lane comes from that credential, and
   its ledger must be the only one allowed in that lane.

   The operator who will drive admission needs a second, separate entry with
   `"capacidades": ["session", "admission.operate"]` — also exactly, since the
   operator routes compare the session's capabilities by set equality.

**Where the fail-fast actually is.** The container-side paths
(`LLMINBOX_JOURNAL`, `LLMINBOX_PEPPER_FILE`, `LLMINBOX_CREDENCIALES`,
`LLMINBOX_ROSTER`, `LLMINBOX_LEDGER_PILOTO`, `LLMINBOX_DB`) are fixed literals in
the compose file — you do not set them and you should not. What carries a
`${VAR:?message}` and therefore fails at **render** time, before anything
starts, is exactly what you supply: the two volume identities and their two
witnesses, the credential-map sha, the pilot token, and the host paths for the
two ledgers, the map, the roster and — in `docker-compose.pilot-key-init.yml` —
the two secret files. A missing one of those names itself and nothing runs.

## Secrets: how they reach the container

`docker-compose.pilot-key-init.yml` copies each host secret into its own named
volume, from which the gateway mounts it read-only. Each init service runs as
root with `network_mode: none`, `read_only: true`, `cap_drop: ALL` and
`no-new-privileges`, and gets back exactly two capabilities:

- **`CHOWN`** — to hand the copy to the unprivileged service account
  (`LLMINBOX_SECRET_UID:LLMINBOX_SECRET_GID`, default `1000:1000`); the gateway
  runs as `1000:1000` and could not otherwise read a root-owned `0600` file.
- **`DAC_OVERRIDE`** — to read the `0600` source and write into the volume
  across that ownership change.

Root with two capabilities and no network is the small end of the trade: the
alternative is a world-readable secret or a service running as root.

It refuses a source file that is not already `0600`, writes the copy `0600`, and
leaves the volume read-only for the gateway.

```bash
docker compose -f docker-compose.pilot-key-init.yml --env-file ~/pilot-m1/pilot.env run --rm pepper-init
docker compose -f docker-compose.pilot-key-init.yml --env-file ~/pilot-m1/pilot.env run --rm projector-key-init  # native mode only
```

## Validate the recipe without starting anything

<!-- doctest:run -->
```bash
test -f docker-compose.pilot.yml && test -f docker-compose.pilot-native.yml \
  && test -f docker-compose.pilot-key-init.yml \
  && test -f docker-compose.pilot-estreno.yml \
  && test -x tools/pilot_preflight.py && test -x scripts/m4-rollback.sh
```

Render both topologies — the overlay has variables of its own, so validating the
pilot alone does not validate native mode:

```bash
docker compose -f docker-compose.pilot.yml \
  --env-file tests/pilot/env.example config -q
docker compose -f docker-compose.pilot.yml -f docker-compose.pilot-native.yml \
  --env-file tests/pilot/env.example config -q
docker compose -f docker-compose.pilot-key-init.yml \
  --env-file tests/pilot/env.example config -q
docker compose -f docker-compose.pilot-estreno.yml \
  --env-file tests/pilot/env.example config -q
```

`tests/pilot/env.example` holds deliberately false values: it exists so this
validation can run at all. Never put a real value in it — the credential map is
indexed by the credential in clear.

## First run (estreno)

The two witnesses are minted here, and this is the step most likely to fail if
you skip it.

**① Align ownership.** Named volumes are born `root:root` and the gateway runs
as `1000:1000`, so it cannot write them. Do this once, before the first run:

```bash
docker run --rm -v llminbox-pilot-m1_llminbox-pilot-journal:/v alpine chown -R 1000:1000 /v
docker run --rm -v llminbox-pilot-m1_llminbox-pilot-index:/v   alpine chown -R 1000:1000 /v
chown -R 1000:1000 "$LLMINBOX_PILOT_LEDGER_HOST"        # the read-write bind
```

Skipping this fails closed with a named `EACCES` and corrupts nothing — that is
the good side of the error, not a reason to skip it.

**② Generate one identity per volume**, and put them in `pilot.env` as
`LLMINBOX_JOURNAL_VOLUME_ID` and `LLMINBOX_LEDGER_PILOTO_ID`:

```bash
python3 tools/pilot_preflight.py --genera-id     # run twice, once per volume
```

`--genera-id` prints a uuid and checks nothing; it is the one mode that is
deployment utility rather than gate, which is why it refuses to be combined with
any other mode.

**③ Mint the witnesses with the dedicated setup service.** Its compose file
uses the same project name and journal volume as the pilot. It requires the two
identities from step ② and the pilot ledger's host path, and mounts only the
journal, that ledger and the local tools. It does not need the witnesses,
credential map, pepper, roster or second ledger:

```bash
docker compose -f docker-compose.pilot-estreno.yml --env-file ~/pilot-m1/pilot.env \
  run --rm estreno
```

It prints, on success, the two lines to keep:

```
LLMINBOX_JOURNAL_VOLUME_WITNESS=…
LLMINBOX_LEDGER_PILOTO_WITNESS=…
```

Put both in `pilot.env`. From now on every start verifies them, and a brand-new
empty volume with the right name fails — which is the point of having a witness
as well as a device check.

On an already initialised volume the setup validates the existing witnesses
and returns their fingerprints. Retain those values; do not replace them with
placeholders to make the gateway render.

## Start

```bash
# pilot, bridge policy
docker compose -f docker-compose.pilot.yml --env-file ~/pilot-m1/pilot.env up -d

# pilot, native mode: native-required policy, projector active, native root
docker compose -f docker-compose.pilot.yml -f docker-compose.pilot-native.yml \
  --env-file ~/pilot-m1/pilot.env up -d
```

The entrypoint runs `tools/pilot_preflight.py` first, in both modes, and only
execs the server if its six checks pass: journal on its own device, journal
witness, pepper, attested map, a single read-write ledger with its witness, and
the projector.

**After that the two modes watch themselves differently, and the difference
matters when something degrades at runtime:**

| | healthcheck | what a red does |
|---|---|---|
| pilot | `pilot_preflight.py --readiness` | re-checks the six preconditions and **kills PID 1** on the first red |
| pilot + native overlay | `GET /ready` | reports `not_ready`; **nothing is killed** |

The overlay declares its own healthcheck and sets **every** key of it — `test`,
`interval`, `timeout`, `retries`, `start_period` — so nothing of the pilot's
probe survives: in native mode the readiness re-check and its cut are gone. A
precondition that rots *after* startup — a witness overwritten, a journal
remounted — is caught in the pilot and is not caught here. In the pilot,
`retries` governs only the `unhealthy` label; the cut happens on the first real
red, with no counter.

## Ready, admission and the operator routes

Native mode's healthcheck also polls `/ready`, which answers
`{"name": "coordination", "state": "ready"}` only when the runtime is usable and
the lane is admitted; otherwise `not_ready`. It is a gate, not a label: a service
that answers `not_ready` is a service that will not take work.

The native root mounts four operator routes, and **only in native mode** — the
quick start and the pilot's default entrypoint serve the legacy app, which does
not mount them. There is no operator console in this repository; these routes
are the interface:

```
GET  /native/v1/operator/admission
POST /native/v1/operator/admission/transition
GET  /native/v1/operator/rollback/status
POST /native/v1/operator/rollback/certify
```

They take a runtime **session**, not a raw credential, and the session's
capabilities must be **exactly** `admission_operator` — a session that also
carries anything else is refused. Open one with the operator credential from
your V8 map, then use the bearer it returns. Note the channel: native routes
carry both the credential and the session in `Authorization: Bearer`, and they
**reject** the legacy `X-Llminbox-Token` header outright.

```bash
PORT=${LLMINBOX_PILOT_PORT:-8099}

SESSION=$(curl -fsS -X POST http://127.0.0.1:$PORT/native/v1/sessions \
  -H "Authorization: Bearer $OPERATOR_CREDENTIAL" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# Read the current state. Two rows come back, one per admission verb:
#   {"admissions":[{"lane":…,"verb":"events.accept","state":"closed","epoch":7},
#                  {"lane":…,"verb":"outbox.requeue","state":"closed","epoch":7}]}
curl -fsS http://127.0.0.1:$PORT/native/v1/operator/admission \
  -H "Authorization: Bearer $SESSION" -o admission.json

# Build the compare-and-set from those rows — keyed by VERB, never by lane.
EPOCHS=$(python3 -c 'import json;d=json.load(open("admission.json"));print(json.dumps({a["verb"]:a["epoch"] for a in d["admissions"]}))')

curl -fsS -X POST http://127.0.0.1:$PORT/native/v1/operator/admission/transition \
  -H "Authorization: Bearer $SESSION" -H 'Content-Type: application/json' \
  -d "{\"target\":\"open\",\"expected_epochs\":$EPOCHS,\"reason_code\":\"ROLLOUT\"}"
```

Four things that bite here.

**`expected_epochs` is keyed by admission verb, not by lane.** The closed pair is
`events.accept` and `outbox.requeue`, and the object must carry **exactly those
two keys**, each a non-negative integer — one key, three keys, or a lane name
instead is refused before anything moves. The two verbs transition together, in
one compare-and-set: opening one door and leaving the other is not expressible.

**It is a compare-and-set, and that is the point.** Send the epochs you just
read; if anything changed in between, your transition fails instead of silently
winning.

**`reason_code` is a closed vocabulary, not free text**: `ROLLOUT`, `INCIDENT`,
`MAINTENANCE`, `DRAIN_FOR_ROLLBACK`. (`SCHEMA_MIGRATION` exists in the schema but
is reserved for the migration itself and is refused from these routes — it would
let an operator forge a migration-shaped record.) The field is durable and
auditable, which is why it will not take a sentence with a path or an id in it.

**The lane is never taken from the body** — it is derived from the session, so
you cannot open someone else's lane by naming it. And `target: "open"`
additionally requires the projector runner to be ready; if it is not, the call
returns a not-ready response and opens nothing. `sealed` is terminal: there is no
transition out of it.

## Rollback

`scripts/m4-rollback.sh` is deliberately more refusal than action, in two phases:

- **Phase 1, with the service alive** — `--a <image> [--datos <snapshot>]
  [--digest <sha>]`: it runs the gates that need a live application and freezes
  everything phase 2 will need into a sealed plan (target image, digest,
  snapshot and its sha, container id, volume, mountpoint, database path),
  derived by inspecting the running service rather than from environment
  variables that can change between phases.
- **Phase 2, with the service stopped** — it applies that plan. The plan's own
  `sha256` is the resume token: editing one comma invalidates it.

**Data rollback is a separate decision from image rollback.** Binary-only
restores the image and leaves the index where it is; binary plus index also
restores the index and loses everything written since that snapshot. Choose on
purpose.

## What this runbook does not certify

- **It has not been executed.** No build, no container, no volume. It is the
  procedure the source describes, pending a run on a candidate build.
- Reading that a gate refuses correctly is not the same as having watched it
  pass. The pilot's gates are fail-closed and, on an untouched tree, are
  expected to say no.
- Reproducibility of the image is enabled, not demonstrated: falsifying it costs
  two builds of the same commit, the second with `--no-cache`, and a digest
  comparison.
- The preflight is mounted from `./tools` rather than copied into the image. A
  release image does that with a `COPY`; the mount is a declared gap, not a
  design.
- Nothing here says the pilot is ready for the fleet-wide deployment. It is one
  lane, one ledger, and a negative control.

Related, and shipped with this repository:
[`docs/PILOT-M1-TOPOLOGY.md`](../PILOT-M1-TOPOLOGY.md) for the topology and its
declared debts, and
[`docs/architecture/coordination-kernel.md`](../architecture/coordination-kernel.md)
for the kernel these services implement.
