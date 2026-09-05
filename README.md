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

```
                     jittered per-merchant           random gateway            random issuer
                     start + send timing              processing delay          processing delay
┌─────────────────┐        ISO 8583        ┌──────────────────┐    ISO 8583    ┌───────────────────┐
│ merchant-        │ ───── auth request ──▶│ acquirer-gateway  │ ── forward ──▶ │ issuer-simulator   │
│ simulator        │                        │                   │                │ (all issuers, one  │
│ (all merchants,  │◀──── auth response ────│  translates PIN   │◀── response ───│  container)        │
│  one container)  │       (mutated by       │  block: terminal  │   (mutated:                        │
└─────────────────┘        the issuer)      │  ZPK → issuer ZPK │    response code,                   │
                                              │  persists to DB   │    auth code)                       │
                                              └─────────┬─────────┘                                      │
                                                         │ INSERT / UPDATE                                │
                                                         ▼                                                │
                                              ┌──────────────────┐        lifecycle events (best-effort)  │
                                              │   PostgreSQL      │        ┌──────────────────────────────┘
                                              │  (swappable, see  │        ▼
                                              │  below)            │  ┌───────────────┐   WebSocket   ┌────────────┐
                                              └──────────────────┘  │  dashboard     │──────────────▶│  browser   │
                                                                     │ (stats, pause/ │◀── pause/resume/rate ──────┘
                                                                     │  resume, rate) │
                                                                     └───────────────┘
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

- Live TPS (5-second rolling window) and approve/decline counts +
  percentages, updated over a WebSocket.
- A live-scrolling feed of completed transactions.
- **Pause / Resume** — stops the merchant-simulator from starting *new*
  transactions; in-flight ones complete normally. Takes effect within a
  couple of seconds (merchants poll for control state every ~2s).
- **Rate slider (0.1x–10x)** — multiplies every merchant's base send
  rate, which itself is proportional to how many template transactions
  that merchant has in the seed pool.

The TPS chart uses Chart.js from a CDN; if that's blocked (offline
environment, restrictive network policy) the chart panel shows a
message instead, but stats, the feed, and pause/resume/rate all keep
working — a CDN outage was deliberately not allowed to take down the
rest of the page.

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
