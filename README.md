# Merchant Acquiring Payment Data — Encryption POC Test Set

[![CI](https://github.com/credulous67/test_acquirer_data/actions/workflows/ci.yml/badge.svg)](https://github.com/credulous67/test_acquirer_data/actions/workflows/ci.yml)

Synthetic test data for a merchant-acquiring encryption POC covering
three common protection patterns: application-level field/column
encryption, gateway/API tokenization, and transparent file/directory
encryption at the storage layer — regardless of vendor.

## Important: this data is entirely fake

- **PANs** are built from publicly documented test/sandbox BIN prefixes
  (the same ranges card networks and processors like Stripe/Braintree
  publish for sandbox use — e.g. `4242 42..`, `4111 11..`, `5555 55..`,
  `3782 82..`) with randomised trailing digits and a correctly computed
  Luhn check digit. They are structurally valid 16/15/14-digit numbers
  but **do not correspond to any real, issued account**. All 20,000
  generated PANs were Luhn-validated after generation (see below).
- Cardholder names, CVVs, expiry dates, track2 data, merchants and
  addresses are randomly generated and do not reference real people,
  businesses, or accounts.
- Do not submit any of this data to a live card network, processor, or
  production system — it's for encryption-at-rest / in-transit /
  in-use testing only, in non-production environments.

## Regenerating / resizing the data

```
python3 scripts/generate_data.py --out data \
    --merchants 250 --cards 20000 --authorizations 200000 --days-back 30 --seed 42
```

The script is deterministic for a given `--seed`. Increase
`--authorizations` / `--cards` / `--merchants` for a larger volume run.
No third-party dependencies — stdlib only (`sqlite3`, `csv`, `json`,
`xml.etree`).

## Layout

```
data/
├── db/                                 Form 1: database-ingestion
│   ├── schema.sql                      DDL (4 tables, sensitive columns flagged in comments)
│   ├── payments.db                     pre-populated SQLite DB (ready to query/attach)
│   └── csv/
│       ├── merchants.csv
│       ├── terminals.csv
│       ├── cards.csv                   card vault: pan, cvv, track2, cardholder_name, expiry
│       └── authorizations.csv          200,000 auth records — only pan, expiry_date and
│                                        card_network carried over from the card; no
│                                        cardholder_name/cvv/track2/card_id (see below)
│
├── structured/                         Form 2: same data as structured files
│   ├── json/
│   │   ├── authorizations/<merchant_id>/<yyyy-mm-dd>/<txn_id>.json
│   │   │                               200,000 individual files across ~250 merchant dirs
│   │   │                               x ~30 date dirs — useful for exercising transparent,
│   │   │                               storage-layer per-directory / per-file encryption
│   │   │                               policies rather than one large blob
│   │   └── authorizations_all.json     same 200,000 records as one JSON array
│   └── xml/
│       └── authorizations.xml          same 200,000 records as ISO8583-flavoured XML
│
└── reference/                          Form 3: generic payment reference data
    ├── mcc_codes.csv                   25 common merchant category codes
    ├── currency_codes.csv              ISO 4217 numeric/alpha codes used in the data
    ├── response_codes.csv              ISO8583-style auth response codes (approve/decline reasons)
    ├── bin_ranges.json                 the test BIN prefixes used, marked TEST_SANDBOX_ONLY
    ├── card_networks.json              PAN/CVV length + Luhn facts per network
    └── merchants/<merchant_id>/
        ├── profile.json                non-sensitive merchant metadata
        └── terminals.csv               that merchant's terminal(s)
```

`reference/` is deliberately free of cardholder data — it's split out
from `db/` and `structured/` so you can test differentiated encryption
policies (e.g. encrypt everything under `db/` and `structured/`, leave
`reference/` in clear text) the way a transparent, storage-layer
encryption deployment would typically be scoped by directory.

## Data volumes (default run)

| Entity | Count |
|---|---|
| Merchants | 250 |
| Terminals | ~640 |
| Cards | 20,000 |
| Authorizations | 200,000 (spanning the last 30 days) |

At this scale the generated `data/` directory is ~1.3 GB, mostly from
the 200,000 individual per-transaction JSON files under
`structured/json/authorizations/`. Generation takes roughly a minute.
Pass smaller `--merchants`/`--cards`/`--authorizations` values for a
quicker, lighter-weight dataset.

## Field dictionary

`cardholder_name`, `cvv`, `track2` and the `card_id` surrogate key live
**only** on the `cards` table — they're not part of what a merchant's
terminal receives back in an ISO 8583 authorization message, so they
aren't denormalized onto `authorizations`. Join `authorizations.pan`
to `cards.pan` if you need to look one up from a transaction.

| Field | Table(s) | Notes |
|---|---|---|
| `pan` | cards, authorizations | Full synthetic PAN, Luhn-valid, from a test BIN prefix |
| `cvv` | cards only | 3 digits (4 for Amex) — normally out-of-scope for storage post-auth even by an issuer; kept only on the card vault for encryption-tooling coverage |
| `track2` | cards only | `PAN=YYMMservice_codediscretionary` |
| `cardholder_name` | cards only | Fake name — not carried in an authorization message |
| `card_id` | cards only | Internal surrogate key; no ISO 8583 equivalent |
| `expiry_date` | cards, authorizations | `MM/YY`, always after the transaction date |
| `response_code` / `response_text` / `response_status` | authorizations | ISO8583-style field 39 equivalents, ~70% approval rate |
| `avs_result` / `cvv_result` | authorizations | Single-letter verification *result* codes returned in the auth response (not the CVV value itself) |

Full column list and types: see `data/db/schema.sql`.

## Suggested test uses

- **Application-level field/column encryption**: encrypt/tokenize
  `pan`, `cvv`, `track2`, `cardholder_name` in `cards.csv` (or the
  SQLite `cards` table) and `pan` in `authorizations`; verify
  format-preserving/tokenized PAN values still join correctly between
  the two tables.
- **Gateway/API tokenization**: replay `authorizations_all.json` or
  the per-file JSON records as simulated API payloads through a
  tokenization proxy; check masked/tokenized output.
- **Transparent file/directory encryption**: point policies at
  `data/db/`, `data/structured/json/authorizations/`, and
  `data/structured/xml/` (sensitive) versus `data/reference/`
  (non-sensitive) and confirm access/encryption behaves per directory.
