# Merchant Acquiring Payment Data — Encryption POC Test Set

Synthetic test data for a merchant-acquiring encryption POC covering
field/column-level encryption (CADP-style), gateway/API tokenization
(CDP-style), and file/directory transparent encryption (CTE-style) —
or the equivalent products from other vendors.

## Important: this data is entirely fake

- **PANs** are built from publicly documented test/sandbox BIN prefixes
  (the same ranges card networks and processors like Stripe/Braintree
  publish for sandbox use — e.g. `4242 42..`, `4111 11..`, `5555 55..`,
  `3782 82..`) with randomised trailing digits and a correctly computed
  Luhn check digit. They are structurally valid 16/15/14-digit numbers
  but **do not correspond to any real, issued account**. All 400
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
│       └── authorizations.csv          800 auth records, denormalized with card data included
│
├── structured/                         Form 2: same data as structured files
│   ├── json/
│   │   ├── authorizations/<merchant_id>/<yyyy-mm-dd>/<txn_id>.json
│   │   │                               800 individual files across ~25 merchant dirs x
│   │   │                               ~30 date dirs — useful for exercising CTE-style
│   │   │                               per-directory / per-file transparent encryption
│   │   │                               policies rather than one large blob
│   │   └── authorizations_all.json     same 800 records as one JSON array
│   └── xml/
│       └── authorizations.xml          same 800 records as ISO8583-flavoured XML
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
`reference/` in clear text) the way a real CTE deployment would be
scoped by directory.

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

## Field dictionary (cards / authorizations)

| Field | Notes |
|---|---|
| `pan` | Full synthetic PAN, Luhn-valid, from a test BIN prefix |
| `pan_masked` | First 6 + last 4, middle masked — for comparing masked vs. unmasked encryption targets |
| `cvv` | 3 digits (4 for Amex) — normally out-of-scope for storage; included here only because this is synthetic test data for encryption tooling |
| `track2` | `PAN=YYMMservice_codediscretionary` |
| `cardholder_name` | Fake name |
| `expiry_date` | `MM/YY`, always after the transaction date |
| `response_code` / `response_text` / `response_status` | ISO8583-style field 39 equivalents, ~70% approval rate |
| `avs_result` / `cvv_result` | Single-letter verification result codes |

Full column list and types: see `data/db/schema.sql`.

## Suggested test uses

- **CADP-style app-level encryption**: encrypt/tokenize `pan`, `cvv`,
  `track2`, `cardholder_name` columns in `cards.csv` / `authorizations.csv`
  or in the SQLite DB; verify format-preserving/tokenized values still
  join correctly across `cards` ↔ `authorizations`.
- **CDP-style gateway tokenization**: replay `authorizations_all.json`
  or the per-file JSON records as simulated API payloads through a
  tokenization proxy; check masked/tokenized output.
- **CTE-style transparent file/directory encryption**: point policies
  at `data/db/`, `data/structured/json/authorizations/`, and
  `data/structured/xml/` (sensitive) versus `data/reference/`
  (non-sensitive) and confirm access/encryption behaves per directory.
