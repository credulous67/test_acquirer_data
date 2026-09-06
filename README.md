# Merchant Acquiring Authorization Simulation — Encryption POC

[![CI](https://github.com/credulous67/test_acquirer_data/actions/workflows/ci.yml/badge.svg)](https://github.com/credulous67/test_acquirer_data/actions/workflows/ci.yml)

A live, running simulation of the merchant → acquirer gateway → card
issuer authorization flow, built to exercise encryption/tokenization/PIN
security tooling **in transit and in use**, not just at rest — three
common protection patterns, regardless of vendor:

- **Application-level field/column encryption** — e.g. the gateway's
  `authorizations` table in Postgres.
- **Gateway/API tokenization** — the ISO 8583 traffic between merchant,
  gateway and issuer.
- **Transparent, storage-layer encryption** — the seed data files and
  the database volume on disk.

Everything runs as a small set of containers under `podman-compose`, with
a web dashboard showing the live flow, TPS, and approve/decline rates.

## Important: this is entirely fake, and two parts are intentionally insecure

- **PANs** are built from publicly documented test/sandbox BIN prefixes
  (the same ranges card networks and processors like Stripe/Braintree
  publish for sandbox use — e.g. `4242 42..`, `4111 11..`, `5555 55..`,
  `3782 82..`) with randomised trailing digits and a correctly computed
  Luhn check digit. They are structurally valid but **do not correspond
  to any real, issued account**.
- Cardholder names, CVVs, PINs, expiry dates, track2 data, merchants and
  addresses are randomly generated and do not reference real people,
  businesses, or accounts.
- Do not point any of this at a live card network, processor, or
  production system.

Two things this project does are things a *real* payment system would
never do, and both exist purely so the simulation can run as plain
software with no HSM hardware:

1. **`data/keys/keys.json` holds AES ZPKs in the clear on disk.** A real
   ZPK only ever exists inside an HSM boundary and is never written to a
   filesystem in the clear.
2. **`data/seed/customer_pins.json` is a test oracle** standing in for
   "what the fake cardholder types at the PIN pad" (keyed by PAN, the one
   thing a terminal actually has). A real merchant/terminal has no
   equivalent file — it only ever captures a live PIN entry that it never
   persists anywhere.

## Architecture

```mermaid
flowchart LR
    M["<b>merchant-simulator</b><br/>all merchants, one container<br/><i>jittered per-merchant start + send timing</i><br/>random gateway replica per transaction"]
    subgraph GW [" "]
        direction TB
        G1["<b>gateway-1</b>"]
        G2["<b>gateway-2</b>"]
    end
    subgraph ISS [" "]
        direction TB
        I1["<b>issuer-simulator-1</b>"]
        I2["<b>issuer-simulator-2</b>"]
    end
    DB[("PostgreSQL<br/>swappable, see below")]
    D["<b>dashboard</b><br/>stats, pause/resume, rate"]
    B["browser"]

    M -- "ISO 8583 auth request" --> GW
    GW -- "forward (ISO 8583),<br/>random issuer replica per txn" --> ISS
    ISS -- "response (mutated:<br/>response code, auth code)" --> GW
    GW -- "auth response<br/>(mutated by issuer)" --> M
    GW -- "INSERT / UPDATE" --> DB
    GW -. "lifecycle events (best-effort)" .-> D
    D -- "WebSocket" --> B
    B -. "pause / resume / rate" .-> D
```

Each gateway replica applies a random processing delay, translates any
PIN block from the terminal's ZPK to the issuer's transit ZPK, and
persists to the shared Postgres instance; each issuer-simulator replica
applies its own random processing delay and holds every issuer's cards
and keys (they're identical, stateless processes, not sharded by
issuer). The gateway tier is the only part of the system that sees every
hop of a transaction, so it's the sole source of the lifecycle events
the dashboard displays. The merchant-simulator polls the dashboard for
the current pause/rate state before sending each transaction.

### Throughput control: `CONCURRENCY_LIMIT` and connection pooling

The gateway bounds how many transactions it processes at once with an
`asyncio.Semaphore`, sized by the `CONCURRENCY_LIMIT` env var (default
150). Without this, the merchant-simulator's fire-and-forget sends have
no cap on how many transactions can be in flight simultaneously; under
sustained high offered load, a small rise in per-transaction latency lets
more and more pile up before any of them finish, which drives latency up
further — a feedback loop that eventually exhausts the DB connection pool
no matter how large it's sized (this was found the hard way: raising the
pool from 80 to 200 connections just delayed the same collapse by half an
hour instead of preventing it). The semaphore is the actual fix: once
`CONCURRENCY_LIMIT` transactions are being processed, a new one simply
waits for a slot — consuming nothing from the DB pool until then — so
offered load self-limits to what the gateway can actually sustain.

Both hops of the merchant→gateway→issuer path reuse persistent TCP
connections (`ConnectionPool` in `services/common/pool.py`) instead of
paying a fresh handshake per transaction:

- the gateway's connections to the issuer, one pool per issuer replica
  (sized to `CONCURRENCY_LIMIT`) — the issuer-simulator's connection
  handler loops, reading further requests off the same connection
  rather than handling one and closing;
- the merchant-simulator's connections to the gateway, one pool per
  gateway replica (sized to `MAX_OUTSTANDING`, see below) — the
  gateway's `handle_merchant()` loops the same way on its side.

A connection that errors on write or read (including the far side
restarting) is discarded rather than returned to its pool, and the
caller falls back to the same decline/drop handling a hard outage would
always have produced — `91` (issuer unreachable) or `96` (generic
failure) from the gateway, or a dropped/logged transaction from the
merchant-simulator — rather than hanging or crashing. Pools recreate
connections on demand up to their cap as old ones are discarded.

Pooling the merchant→gateway leg specifically was added after measuring
it as a real inefficiency, not just a hypothetical one: under a 10x load
test *before* this leg was pooled, `gateway-1`'s merchant-facing port
showed 4412 TIME_WAIT sockets against only 254 ESTABLISHED — a fresh
TCP connection was being opened and torn down for every single
transaction. After pooling it, the same test showed 0 TIME_WAIT on that
port.

That same investigation turned up a second, unrelated source of
connection churn: `report_event()`'s `httpx.AsyncClient` posts to the
dashboard showed 6062 TIME_WAIT against only 20 ESTABLISHED under load —
the 20 an exact match for httpx's own default `max_keepalive_connections`.
httpx already pools connections internally; its default pool was just
sized for a low-concurrency client, not one with up to `CONCURRENCY_LIMIT`
transactions each posting events around the same time. Every `httpx.
AsyncClient` in this system (gateway, merchant-simulator, issuer-simulator)
now passes an explicit `httpx.Limits` sized to its actual concurrency —
`CONCURRENCY_LIMIT` for the gateway's client, a smaller fixed headroom
for the other two, whose control-plane polling is cached and far lower
volume. `services/common/control.py`'s `ControlPoller` also gained a
lock around its cache refresh, so many callers seeing the cache go stale
in the same instant coalesce into one dashboard request instead of each
firing their own. Re-running the same 10x load test afterwards showed
0 TIME_WAIT on the gateway's dashboard connection throughout.

### Fire-and-forget dashboard events, and a self-inflicted CPU bottleneck

The gateway's `fire_event()` posts each stage of a transaction
(`received`, `forwarded_to_issuer`, `completed`) to the dashboard as a
background `asyncio.create_task` rather than being `await`ed inline in
`_handle_merchant()`. `report_event()` itself already never raises — a
dashboard outage or slow response must never block the authorization
path — but it used to be awaited anyway, so every transaction held its
`CONCURRENCY_LIMIT` slot for the full round trip to the dashboard on top
of its own processing. Firing it as a background task instead shortens
each transaction's real time-in-system, raising throughput for the same
concurrency limit. As with the merchant-simulator's own background sends
(`_BACKGROUND_TASKS`), a strong reference (`_event_tasks`) keeps each
task alive until it finishes, since asyncio only holds a *weak* one
otherwise and could silently garbage-collect it mid-flight.

The first version of this fix made things *worse*, and it's worth
recording why: making every event fire-and-forget let far more of them
pile up concurrently against the gateway's single `httpx.AsyncClient`
than before, when a transaction's own 3 event posts were naturally
serialized by being awaited one after another. `py-spy dump` on a
gateway process pegged at ~90% CPU while producing almost no throughput
showed the entire main thread stuck in httpcore's
`_assign_requests_to_connections` — the connection-pool bookkeeping that
matches pending requests to available connections doesn't scale well
with a large number of simultaneously in-flight requests on one client,
and became a CPU-bound bottleneck in its own right, completely
independent of `CONCURRENCY_LIMIT` or anything DB/issuer-related. The
fix was `_event_semaphore`, capping actual concurrent httpx dispatch to
30 — the caller still never waits on it (only the background task does),
so the non-blocking benefit is kept, but httpx's own bookkeeping stays
cheap. This is the reason `fire_event()`'s implementation is two small
functions (`fire_event` creates the task and returns immediately;
`_dispatch_event` is what actually waits on the semaphore) rather than
one.

### Horizontal scaling: multiple gateway and issuer-simulator replicas

`podman-compose.yml` runs the gateway and issuer-simulator each as 2
named, identical replicas (`gateway-1`/`gateway-2`,
`issuer-simulator-1`/`issuer-simulator-2`) rather than one instance of
each. Nothing in either service is stateful across a single
transaction's handling — a gateway replica's only shared state is the
Postgres database and the dashboard, both already designed to be written
to from anywhere — so any number of identical processes can run side by
side.

Rather than relying on a compose tool's `--scale` plus DNS round-robin
across replicas of one service name (unreliable under podman's embedded
DNS in practice), each replica gets its own service name, and whichever
service talks to it is handed every replica's `host:port` explicitly via
a comma-separated `*_HOSTS` env var (parsed by
`services/common/util.parse_host_list`), picking one at random per
transaction:

- `merchant-simulator`'s `GATEWAY_HOSTS` (default
  `"gateway-1:8583,gateway-2:8583"`) — which gateway replica's
  `ConnectionPool` a transaction is sent through.
- `gateway-*`'s `ISSUER_HOSTS` (default
  `"issuer-simulator-1:8584,issuer-simulator-2:8584"`) — which
  issuer-simulator replica's `ConnectionPool` a gateway forwards
  through (see above).

A replica that's down just fails its connect/read and gets the same
"91 issuer unreachable" or dropped-transaction handling a single-replica
outage always had — nothing here assumes every replica is healthy.

**To add a replica**: copy the relevant service block in
`podman-compose.yml` (e.g. `gateway-2`) to a new name (`gateway-3`), add
its `host:port` to the consuming service's `*_HOSTS` env var, and — for
an added gateway replica specifically — raise Postgres's
`max_connections` to cover the extra replica's `CONCURRENCY_LIMIT` (see
`services/gateway/db.py`'s pool-sizing comment; each gateway replica's DB
pool is sized to its own `CONCURRENCY_LIMIT`, so N replicas need
Postgres to allow roughly `N × CONCURRENCY_LIMIT` connections).

Both replicas' schemas race harmlessly on first boot: `init_db()`
catches and logs the duplicate-table error from whichever replica loses
the race to create the `authorizations` table, since the outcome
(schema exists) is what both wanted anyway.

**Measured throughput**: pushing the rate slider to 10x (~100 offered
TPS) and measuring the sustained result directly against Postgres (row
count delta over a timed window, cross-checked against the dashboard's
own TPS stat) gave a ceiling of roughly **70-80 TPS** after connection
pooling and horizontal scaling alone, up from ~42 TPS on a single
gateway/single issuer-simulator before any of the fixes in this section
existed. Fixing the fire-and-forget event-dispatch bottleneck above
pushed that further, to individual readings **peaking at 94 TPS** —
essentially the rate slider's own ceiling — with a noisier sustained
average in the 60-75 TPS range on the 4-core machine this was measured
on; the oscillation itself (rather than a clean plateau) is a sign of
the system now genuinely probing against real CPU capacity rather than
an artificial bottleneck. At these levels the gateway's own concurrency
semaphore is nowhere near saturated (Little's Law: throughput × time-
in-system stays comfortably under the 300 total capacity of 2 replicas ×
`CONCURRENCY_LIMIT` 150) — the constraint is host CPU itself, not any of
the pools, limits, or semaphores described here. Those exist to make the
system degrade honestly under real resource pressure (rising latency, a
growing but bounded backlog, zero errors) rather than to raise the
ceiling itself; on different hardware — or with other processes (a
browser, in one case) competing for the same cores — the ceiling will be
different.

### Merchant-side backpressure: `MAX_OUTSTANDING`

The merchant-simulator originates transactions as independent
fire-and-forget `asyncio` tasks (see `send_one` in
`services/merchant_simulator/app.py`), so a slow gateway/issuer tier
doesn't block a merchant from continuing to schedule new ones at its
target rate. Before this existed, that meant the number of transactions
actually connected and waiting on a response — the true resource cost —
had no cap at all: it could grow without bound if the downstream
couldn't keep up.

`MAX_OUTSTANDING` (default 300) bounds that directly: `send_one` waits
on a global `asyncio.Semaphore` of this size *before* opening its
connection to a gateway, and holds it until the response (or a failure)
comes back. A merchant's own send-pacing loop keeps scheduling new tasks
at its target rate regardless — those tasks just queue on the
semaphore, which costs nothing (no socket, no gateway-side resource) —
so it's specifically the expensive part (open connections into the
gateway tier) that self-limits when the downstream is saturated, the
same principle as the gateway's own `CONCURRENCY_LIMIT` applied one hop
further upstream. The default (300) is sized to roughly match the
gateway tier's total capacity (2 replicas × `CONCURRENCY_LIMIT` 150 each)
so the merchant-simulator won't try to keep more in flight than the
gateway tier could ever process at once.

## Running it

```
podman-compose up --build
```

(`docker compose up --build` works against the same file too.) This:

1. Runs `seed-generator` once to populate a shared volume with merchants,
   terminals, cards (including at-rest-encrypted PIN blocks), a pool of
   authorization request templates, and the ZPKs.
2. Starts Postgres and waits for it to be healthy.
3. Starts the gateway and issuer-simulator replicas and the
   merchant-simulator, which wait for the seed data to exist before
   doing anything.
4. Starts the dashboard on **http://localhost:8080**.

Every service that consumes the seed data polls for its required files
on startup rather than trusting compose's dependency ordering alone —
`condition: service_completed_successfully` support varies across
compose implementations, and this makes the system robust either way.

The seed-generator is idempotent by default (`--if-missing`): if
`data/seed/authorizations.json` already exists in the shared volume it
exits immediately rather than regenerating. This matters because AES
keys are generated with `secrets`, not the `--seed`'d `random` module, so
they differ on every invocation — if a compose tool re-triggers a
one-shot service more than once (some do), a second run would silently
replace `cards.json`'s at-rest PIN blocks and `keys.json`'s ZPKs with a
fresh, mutually inconsistent pair out from under services that already
read the first set.

## Regenerating / resizing the seed data

```
python3 scripts/generate_data.py --out data \
    --merchants 250 --cards 20000 --authorizations 200000 --days-back 30 --seed 42
```

Deterministic for a given `--seed`, except for the AES keys (see above).
No third-party dependencies beyond `cryptography` (stdlib otherwise —
`json`, `random`, `uuid`). Output layout:

```
data/
├── seed/
│   ├── merchants.json
│   ├── terminals.json
│   ├── cards.json                 pan, cvv, track2, cardholder_name, issuer_id,
│   │                               pin_block_at_rest_hex (never a plaintext PIN)
│   ├── authorizations.json        request templates the merchant-simulator replays
│   │                               live (no response fields — those are decided
│   │                               live by the issuer-simulator, not pre-baked)
│   ├── customer_pins.json         TEST-ONLY oracle, see warning above
│   └── customer_cvvs.json         TEST-ONLY oracle: the CVV2 each fake
│                                   cardholder reads off their card for a
│                                   card-not-present purchase
│
├── keys/
│   └── keys.json                  TEST-ONLY plaintext ZPKs (terminal, plus
│                                   a separate transit and storage ZPK per
│                                   issuer -- see "The PIN / ZPK model")
│
└── reference/                     generic, non-cardholder payment reference data
    ├── mcc_codes.json
    ├── currency_codes.json
    ├── response_codes.json
    ├── bin_ranges.json             test BIN prefixes, marked TEST_SANDBOX_ONLY
    ├── card_networks.json
    └── merchants/<merchant_id>/{profile.json, terminals.json}
```

At the default scale this is ~250 merchants, ~640 terminals, 20,000
cards, 200,000 request templates, and takes well under a minute to
generate.

## The PIN / ZPK model

Some transactions carry a PIN (card-present entry modes only — CHIP and
SWIPE at 50%, CONTACTLESS at 15%; never ECOM/MANUAL, which have no PIN
pad). Each terminal has one AES-128 ZPK, and each issuer (one simulated
issuer per card network — `ISSUER-VISA`, `ISSUER-MASTERCARD`, etc; every
issuer-simulator replica hosts all of them, since they're identical
stateless processes, not sharded by issuer) has **two**, not one:

- a **transit ZPK** — the zone key for the gateway↔issuer link, used to
  receive PIN blocks arriving with a live authorization;
- a **storage ZPK** — used only to protect that issuer's own PIN store
  at rest, on `cards.json`'s `pin_block_at_rest_hex`.

These are deliberately different keys. A real issuer never uses its
interchange/zone key to protect data at rest — collapsing them into one
key would mean the "PIN arriving over the wire" trust boundary and the
"PIN vault" trust boundary were silently the same thing, which they must
not be, and PIN validation would then also fail to reflect a real
issuer's actual verification step.

1. The merchant-simulator builds an **ISO 9564-1 Format 4 (AES) PIN
   block** for the entered PIN, enciphered under that **terminal's**
   ZPK (`services/common/crypto.py`). ~3% of the time it perturbs one
   digit first, to simulate a mistyped PIN arriving from the acquirer
   side — a legitimate source of `55` declines alongside the check below.
2. The gateway **translates** the block: decrypts it with the terminal's
   ZPK, then re-enciphers it (with a fresh random pad) under the
   destination issuer's **transit** ZPK, before forwarding to the
   issuer. In a real acquiring host this happens inside an HSM, which
   never releases the clear PIN outside its own boundary; here it's a
   plain Python function because this is a software simulation harness,
   not a certified cryptographic device.
3. The issuer-simulator decrypts the incoming block under its **transit**
   ZPK and the card's at-rest block under its separate **storage** ZPK —
   two independent decryptions, never a plaintext PIN anywhere on disk —
   then compares the two recovered PIN digit strings. This is a "local
   PIN check", one of the two verification styles real issuers use (the
   other, PVV-based verification, needs a separate PIN Verification Key
   this project doesn't model). A mismatch declines with response code
   `55`.

The ISO-4 implementation follows the standard's general structure
(control nibble + PIN length + digits + random padding for the PIN
field, XORed against a PAN-derived field, through AES twice) closely
enough to be internally consistent and reversible end-to-end across this
project's own encode/decode/translate calls — it is not a certified,
byte-for-byte implementation of the standard, and must never be used
outside this test context. See `services/common/test_crypto.py` for the
round-trip/translation tests.

## CVV2 verification

Card-not-present transactions (ECOM and MANUAL entry modes) always carry
a CVV2, read by the fake cardholder off the back of their card — a
different concept from the PIN above, and handled very differently:
never encrypted. The issuer-simulator compares it as a plain string
against the card record's own `cvv` field and declines with `82` on a
mismatch (~3% of the time, from `services/merchant_simulator/app.py`'s
`CVV_TYPO_PROBABILITY`, the same simulated-typo mechanism the PIN uses).

This is a deliberate, realistic asymmetry, not an oversight: PIN blocks
need field-level encryption end-to-end because a PIN grants direct
access to funds and PCI PIN Security requirements treat it accordingly,
even over an already-encrypted transport; CVV2 has no equivalent
requirement and travels in the clear within the authorization message
itself in real systems too (protected only by the transport, e.g. TLS,
which this project doesn't model).

## The ISO 8583 message model

`services/common/iso8583.py` is a minimal, self-contained codec covering
only the fields this simulation needs, using the standard's real field
numbers/formats where one applies cleanly (PAN, processing code, amount,
STAN, expiry, MCC, POS entry mode, response code, auth code, etc.), plus
one private-use field (63, reserved for private use in the standard)
carrying a small JSON blob for project-specific extras (card network,
transaction type, currency alpha code, whether a PIN was entered) that
don't have a clean standard field of their own. Field 52 (PIN data) is a
16-byte binary field here to hold an AES/ISO-4 block; the real
standard's field 52 is traditionally a fixed 8-byte DES-sized block, so
that one field is a project-specific extension. Transport is raw TCP
with a 2-byte big-endian length prefix — the same framing style real
ISO 8583 host-to-host links use.

## The dashboard

**http://localhost:8080** shows:

- Stat cards: live TPS (20-second rolling window), lifetime approve/
  decline counts + percentages (weighted ~95% approve by default, see
  `services/common/reference.GENERIC_RESPONSE_WEIGHTS`), the number of
  **outstanding** authorizations (received but not yet responded to),
  and the **average end-to-end latency** (`received_at` → `completed_at`,
  recorded per transaction as `duration_ms` on the gateway's
  `authorizations` row and in its "completed" dashboard event).
- Three side-by-side charts, all a 30-minute rolling window (one point/sec):
  **Overall TPS** (a single line); **Approved vs declined**; and
  **Auth type** — both of the latter are stacked area charts of each
  category's *share* of the last 20 seconds, always summing to 100% (a
  different, windowed number from the lifetime approve/decline
  percentages in the stat cards above, which barely move once there's
  been a lot of traffic). Auth type is derived from the ISO 8583 POS
  entry mode (`services/common/reference.AUTH_TYPE_LABELS`): EMV (chip),
  Contactless, Magstripe (swipe), and two card-not-present flavours,
  CNP (eCom) and CNP (MOTO).
- A live-scrolling feed of completed transactions, including each one's
  auth type, response code with its plain-English meaning inline (e.g.
  `55 (Incorrect PIN)`, from `services/common/reference.RESPONSE_CODES`
  — the same table the issuer-simulator itself picks from), and
  end-to-end duration.
- **Pause merchant / Pause issuer** — independent controls:
  - *Pause merchant* stops the merchant-simulator from originating *new*
    transactions; already-sent ones complete normally.
  - *Pause issuer* leaves the gateway accepting and forwarding requests
    as normal, but the issuer-simulator holds each connection open
    without responding. This is what lets you watch authorizations pile
    up as "outstanding" with no response — deliberately *not* modelling
    a network-level outage (which would trigger the acquirer's Stand-In
    Processing path in a real system), just an issuer that's up but
    unresponsive at the message level.
  - Both take effect within a couple of seconds (services poll control
    state every ~2s) and are independent of each other.
- **Rate slider (0.1x–10x)** — multiplies every merchant's base send
  rate, which itself is proportional to how many template transactions
  that merchant has in the seed pool. The combined base rate across all
  merchants is `TARGET_TPS` (default 10), so the slider's range spans
  roughly 1–100 offered TPS.

Chart.js is vendored locally (`services/dashboard/static/vendor/`), not
loaded from a CDN — the browser only ever talks to the dashboard
container, so this works with no outbound internet access at all. If
that file were ever missing (e.g. an image build that dropped it), all
three chart panels show a message instead, but the stat cards, the feed,
and pause/resume/rate all keep working regardless.

## Shutting down and resuming cleanly

`podman-compose down`/`stop` sends every container SIGTERM at once, which
is fine most of the time but can leave an authorization stuck mid-flight
(the gateway had inserted it and forwarded it to the issuer, but never
got a response before being killed). Two things make a clean stop/resume
possible:

- Each service traps SIGTERM: the gateway and issuer-simulator stop
  *accepting new* connections but finish any already in flight (see
  `services/common/util.serve_until_signal`), and the merchant-simulator
  stops *originating new* sends but waits for its own in-flight ones to
  get a response, before exiting. `stop_grace_period: 35s` in
  `podman-compose.yml` gives them room to do this before podman would
  otherwise escalate to SIGKILL.
- `scripts/graceful_shutdown.sh` sequences it further at the whole-stack
  level: stop the merchant-simulator first, poll the dashboard's
  `GET /api/stats` until `outstanding` reaches 0 (i.e. the gateway and
  issuer have finished everything already accepted), *then* stop
  gateway/issuer-simulator/dashboard/postgres. Named volumes are never
  touched, so `podman-compose start` afterwards resumes with the same
  seed data and database contents and nothing left stuck mid-authorization.
  Pass `--down` if you also want the containers removed (e.g. before
  rebuilding images) rather than just stopped — volumes are kept either way.

## Swapping the database

The gateway (`services/gateway/db.py`) is built on SQLAlchemy Core with
only portable column types (`String`/`Numeric`/`DateTime`/`Boolean` —
nothing Postgres-specific like `JSONB`), so moving off Postgres is a
config change, not a rewrite:

1. Change `DATABASE_URL` in `podman-compose.yml` (or the environment),
   e.g. `mysql+aiomysql://user:pass@mysql:3306/payments`.
2. Add the matching async driver to `services/gateway/requirements.txt`
   (`aiomysql` or `asyncmy` for MySQL) and swap the `postgres` service
   for the new image.
3. Rebuild the gateway image. The table is created on startup via
   `metadata.create_all`, so no separate migration step is needed for a
   fresh database.
4. Set the new database's own connection-limit setting (MySQL's
   `max_connections`, Postgres's shown above) the same way: roughly
   `N × CONCURRENCY_LIMIT` for N gateway replicas — see "Horizontal
   scaling" above for why.

## Suggested test uses

- **Application-level field/column encryption**: encrypt/tokenize `pan`
  (and, if you extend the schema, other columns) in the gateway's
  Postgres `authorizations` table; verify the gateway and any reporting
  queries still work against the encrypted/tokenized column.
- **In-transit / PIN security tooling**: point tooling at the TCP links
  between merchant-simulator ↔ gateway ↔ issuer-simulator, or at the
  gateway's PIN-translate step itself, to validate ISO-4 PIN block and
  ZPK handling end-to-end.
- **Transparent, storage-layer encryption**: point policies at
  `data/seed/`, `data/keys/`, and the Postgres data volume (sensitive)
  versus `data/reference/` (non-sensitive, deliberately kept separate)
  and confirm access/encryption behaves per directory/volume.
- **Gateway/API tokenization**: sit a tokenization proxy between the
  merchant-simulator and the gateway (or between the gateway and the
  issuer-simulator) and confirm the ISO 8583 traffic still round-trips
  with tokenized PANs.
