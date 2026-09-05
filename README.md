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
    M["<b>merchant-simulator</b><br/>all merchants, one container<br/><i>jittered per-merchant start + send timing</i>"]
    G["<b>acquirer-gateway</b><br/><i>random processing delay</i><br/>translates PIN block:<br/>terminal ZPK → issuer ZPK<br/>persists to DB"]
    I["<b>issuer-simulator</b><br/>all issuers, one container<br/><i>random processing delay</i>"]
    DB[("PostgreSQL<br/>swappable, see below")]
    D["<b>dashboard</b><br/>stats, pause/resume, rate"]
    B["browser"]

    M -- "ISO 8583 auth request" --> G
    G -- "forward (ISO 8583)" --> I
    I -- "response (mutated:<br/>response code, auth code)" --> G
    G -- "auth response<br/>(mutated by issuer)" --> M
    G -- "INSERT / UPDATE" --> DB
    G -. "lifecycle events (best-effort)" .-> D
    D -- "WebSocket" --> B
    B -. "pause / resume / rate" .-> D
```

The gateway is the only component that sees every hop of a transaction,
so it's the sole source of the lifecycle events the dashboard displays.
The merchant-simulator polls the dashboard for the current pause/rate
state before sending each transaction.

## Running it

```
podman-compose up --build
```

(`docker compose up --build` works against the same file too.) This:

1. Runs `seed-generator` once to populate a shared volume with merchants,
   terminals, cards (including at-rest-encrypted PIN blocks), a pool of
   authorization request templates, and the ZPKs.
2. Starts Postgres and waits for it to be healthy.
3. Starts the gateway, issuer-simulator and merchant-simulator, which
   wait for the seed data to exist before doing anything.
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
│   └── customer_pins.json         TEST-ONLY oracle, see warning above
│
├── keys/
│   └── keys.json                  TEST-ONLY plaintext ZPKs, see warning above
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
pad). Each card has one AES-128 ZPK per **terminal** and one per
**issuer** (one simulated issuer per card network — `ISSUER-VISA`,
`ISSUER-MASTERCARD`, etc; the issuer-simulator container hosts all of
them):

1. The merchant-simulator builds an **ISO 9564-1 Format 4 (AES) PIN
   block** for the entered PIN, enciphered under that **terminal's**
   ZPK (`services/common/crypto.py`). ~3% of the time it perturbs one
   digit first, to simulate a mistyped PIN.
2. The gateway **translates** the block: decrypts it with the terminal's
   ZPK, then re-enciphers it (with a fresh random pad) under the
   destination **issuer's** ZPK, before forwarding to the issuer. In a
   real acquiring host this happens inside an HSM, which never releases
   the clear PIN outside its own boundary; here it's a plain Python
   function because this is a software simulation harness, not a
   certified cryptographic device.
3. The issuer-simulator decrypts the incoming block and the card's
   at-rest block (`cards.json`'s `pin_block_at_rest_hex`, itself an
   ISO-4 block enciphered under that same issuer's ZPK — never a
   plaintext PIN anywhere on disk) and compares the recovered PIN
   digits — a "local PIN check", one of the two verification styles real
   issuers use (the other, PVV-based verification, needs a separate PIN
   Verification Key this project doesn't model). A mismatch declines
   with response code `55`.

The ISO-4 implementation follows the standard's general structure
(control nibble + PIN length + digits + random padding for the PIN
field, XORed against a PAN-derived field, through AES twice) closely
enough to be internally consistent and reversible end-to-end across this
project's own encode/decode/translate calls — it is not a certified,
byte-for-byte implementation of the standard, and must never be used
outside this test context. See `services/common/test_crypto.py` for the
round-trip/translation tests.

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

- Stat cards: live TPS (5-second rolling window), lifetime approve/
  decline counts + percentages (weighted ~85% approve by default, see
  `services/common/reference.GENERIC_RESPONSE_WEIGHTS`), the number of
  **outstanding** authorizations (received but not yet responded to),
  and the **average end-to-end latency** (`received_at` → `completed_at`,
  recorded per transaction as `duration_ms` on the gateway's
  `authorizations` row and in its "completed" dashboard event).
- Three side-by-side charts, all a 30-minute rolling window (one point/sec):
  **Overall TPS** (a single line); **Approved vs declined**; and
  **Auth type** — both of the latter are stacked area charts of each
  category's *share* of the last 5 seconds, always summing to 100% (a
  different, windowed number from the lifetime approve/decline
  percentages in the stat cards above, which barely move once there's
  been a lot of traffic). Auth type is derived from the ISO 8583 POS
  entry mode (`services/common/reference.AUTH_TYPE_LABELS`): EMV (chip),
  Contactless, Magstripe (swipe), and two card-not-present flavours,
  CNP (eCom) and CNP (MOTO).
- A live-scrolling feed of completed transactions, including each one's
  auth type and end-to-end duration.
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
  that merchant has in the seed pool.

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
