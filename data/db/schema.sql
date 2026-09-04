-- Synthetic merchant-acquiring schema for encryption POC testing.
-- pan / cvv / track2 / cardholder_name are the columns intended to be
-- protected by CADP / CTE / CDP (or vendor-equivalent) controls.

CREATE TABLE IF NOT EXISTS merchants (
    merchant_id     TEXT PRIMARY KEY,
    legal_name      TEXT NOT NULL,
    mcc             TEXT NOT NULL,
    mcc_description TEXT,
    acquirer_id     TEXT NOT NULL,
    business_type   TEXT,
    onboarded_date  TEXT,
    address_line1   TEXT,
    address_city    TEXT,
    address_country TEXT,
    address_postal  TEXT
);

CREATE TABLE IF NOT EXISTS terminals (
    terminal_id     TEXT PRIMARY KEY,
    merchant_id     TEXT NOT NULL REFERENCES merchants(merchant_id),
    terminal_type   TEXT,
    serial_number   TEXT,
    location        TEXT
);

CREATE TABLE IF NOT EXISTS cards (
    card_id         TEXT PRIMARY KEY,
    pan             TEXT NOT NULL,        -- sensitive: PAN
    pan_masked      TEXT NOT NULL,
    cardholder_name TEXT NOT NULL,        -- sensitive
    expiry_date     TEXT NOT NULL,        -- sensitive
    cvv             TEXT NOT NULL,        -- sensitive, out of scope for storage in real systems (test-only)
    track2          TEXT NOT NULL,        -- sensitive
    card_network    TEXT NOT NULL,
    issuing_bin     TEXT NOT NULL,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS authorizations (
    transaction_id              TEXT PRIMARY KEY,
    merchant_id                 TEXT NOT NULL REFERENCES merchants(merchant_id),
    terminal_id                 TEXT NOT NULL REFERENCES terminals(terminal_id),
    card_id                     TEXT NOT NULL REFERENCES cards(card_id),
    pan                         TEXT NOT NULL,   -- sensitive (denormalized for encryption-at-rest testing)
    pan_masked                  TEXT NOT NULL,
    cardholder_name             TEXT NOT NULL,   -- sensitive
    expiry_date                 TEXT NOT NULL,   -- sensitive
    cvv                         TEXT NOT NULL,   -- sensitive
    track2                      TEXT NOT NULL,   -- sensitive
    card_network                TEXT NOT NULL,
    transaction_type            TEXT NOT NULL,
    amount                      TEXT NOT NULL,
    currency_code_numeric       TEXT NOT NULL,
    currency_code_alpha         TEXT NOT NULL,
    mcc                         TEXT,
    pos_entry_mode              TEXT,
    auth_code                   TEXT,
    response_code               TEXT,
    response_text               TEXT,
    response_status             TEXT,
    stan                        TEXT,
    retrieval_reference_number  TEXT,
    avs_result                  TEXT,
    cvv_result                  TEXT,
    acquirer_id                 TEXT,
    timestamp                   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_merchant ON authorizations(merchant_id);
CREATE INDEX IF NOT EXISTS idx_auth_card ON authorizations(card_id);
CREATE INDEX IF NOT EXISTS idx_auth_timestamp ON authorizations(timestamp);
