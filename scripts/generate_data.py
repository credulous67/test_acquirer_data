#!/usr/bin/env python3
"""
Synthetic merchant-acquiring payment data generator for encryption POC testing
of application-level field encryption, gateway/API tokenization, and
transparent file/directory encryption, regardless of vendor.

ALL data produced by this script is fake:
  - PANs are built from publicly-documented test/sandbox BIN prefixes
    (the same ranges Visa, Mastercard, Amex and processors like Stripe
    publish for sandbox use) with randomised trailing digits and a
    correctly computed Luhn check digit. They are structurally valid
    but are NOT real, issued account numbers.
  - Names, addresses, merchants, CVVs and track data are randomly
    generated and do not correspond to real people or businesses.

Output layout (under --out, default ./data):
  db/schema.sql              DDL for merchants/terminals/cards/authorizations
  db/payments.db             populated SQLite database (ready to query)
  db/csv/*.csv               same tables as CSV, for bulk-load into any RDBMS
  structured/json/authorizations/<merchant_id>/<yyyy-mm-dd>/<txn_id>.json
                              one authorization per file (good for file/
                              directory-level transparent encryption tests)
  structured/json/authorizations_all.json
  structured/xml/authorizations.xml
  reference/*                generic, non-card payment reference data
                              (MCC codes, currency codes, response codes,
                              BIN range table, card network facts)
  reference/merchants/<merchant_id>/profile.json + terminals.csv
                              non-sensitive merchant metadata, split out
                              from the cardholder-data tables on purpose
"""
import argparse
import csv
import json
import os
import random
import sqlite3
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "William", "Elizabeth", "David", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Charles", "Karen", "Chidi", "Amara", "Wei", "Yuki",
    "Priya", "Arjun", "Fatima", "Omar", "Liam", "Olivia", "Noah", "Emma",
    "Lucas", "Sofia", "Ethan", "Ava", "Mateus", "Camila", "Hiroshi", "Sakura",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Okafor", "Nakamura", "Singh",
    "Kowalski", "Dubois", "Rossi", "Santos", "Muller", "Andersson", "Kim",
]

MERCHANT_NAMES = [
    "Riverside Grocers", "Northgate Electronics", "Blue Anchor Diner", "Summit Hardware",
    "Cobalt Fashion Outlet", "Pacific Coast Fuel", "Lantern Books & Coffee", "Ironbridge Motors",
    "Willow Creek Pharmacy", "Metro Transit Kiosk", "Cedar Park Cinema", "Harbor View Hotel",
    "Golden Wheat Bakery", "Silverline Telecom", "Maple & Vine Wine Bar", "QuickStop Convenience",
    "Aurora Home Furnishings", "Sunset Boulevard Salon", "Foothills Sporting Goods", "Crestwood Pet Supply",
    "Union Square Florist", "Bayside Seafood Market", "Redwood Auto Parts", "Emerald City Toys",
    "Prairie Wind Farm Stand",
]

MCC_TABLE = [
    ("5411", "Grocery Stores, Supermarkets"),
    ("5732", "Electronics Stores"),
    ("5812", "Eating Places, Restaurants"),
    ("5251", "Hardware Stores"),
    ("5651", "Family Clothing Stores"),
    ("5541", "Service Stations (Fuel)"),
    ("5942", "Book Stores"),
    ("5511", "Car and Truck Dealers"),
    ("5912", "Drug Stores and Pharmacies"),
    ("4111", "Local/Suburban Commuter Transport"),
    ("7832", "Motion Picture Theaters"),
    ("7011", "Hotels, Motels, Resorts"),
    ("5462", "Bakeries"),
    ("4814", "Telecommunication Services"),
    ("5813", "Bars, Cocktail Lounges"),
    ("5499", "Convenience Stores"),
    ("5712", "Furniture, Home Furnishings"),
    ("7230", "Beauty and Barber Shops"),
    ("5941", "Sporting Goods Stores"),
    ("5995", "Pet Shops, Pet Supplies"),
    ("5992", "Florists"),
    ("5422", "Freezer/Meat/Fish Markets"),
    ("5533", "Auto Parts and Accessories"),
    ("5945", "Hobby, Toy and Game Shops"),
    ("5431", "Farm/Roadside Produce Stands"),
]

CURRENCIES = [
    ("840", "USD", 2, "US Dollar"),
    ("978", "EUR", 2, "Euro"),
    ("826", "GBP", 2, "Pound Sterling"),
    ("124", "CAD", 2, "Canadian Dollar"),
    ("036", "AUD", 2, "Australian Dollar"),
    ("392", "JPY", 0, "Japanese Yen"),
    ("756", "CHF", 2, "Swiss Franc"),
    ("710", "ZAR", 2, "South African Rand"),
    ("356", "INR", 2, "Indian Rupee"),
    ("484", "MXN", 2, "Mexican Peso"),
    ("986", "BRL", 2, "Brazilian Real"),
    ("702", "SGD", 2, "Singapore Dollar"),
]

RESPONSE_CODES = [
    ("00", "Approved", "APPROVED"),
    ("01", "Refer to card issuer", "DECLINED"),
    ("04", "Pick up card", "DECLINED"),
    ("05", "Do not honor", "DECLINED"),
    ("12", "Invalid transaction", "DECLINED"),
    ("14", "Invalid card number", "DECLINED"),
    ("30", "Format error", "DECLINED"),
    ("41", "Lost card", "DECLINED"),
    ("43", "Stolen card", "DECLINED"),
    ("51", "Insufficient funds", "DECLINED"),
    ("54", "Expired card", "DECLINED"),
    ("57", "Transaction not permitted to cardholder", "DECLINED"),
    ("58", "Transaction not permitted to terminal", "DECLINED"),
    ("61", "Exceeds withdrawal amount limit", "DECLINED"),
    ("62", "Restricted card", "DECLINED"),
    ("65", "Exceeds withdrawal frequency limit", "DECLINED"),
    ("75", "PIN tries exceeded", "DECLINED"),
    ("91", "Issuer or switch inoperative", "DECLINED"),
    ("96", "System malfunction", "DECLINED"),
]
# weighted toward approvals, like a real portfolio
RESPONSE_WEIGHTS = [70] + [30 / (len(RESPONSE_CODES) - 1)] * (len(RESPONSE_CODES) - 1)

TXN_TYPES = ["PURCHASE", "PURCHASE", "PURCHASE", "PURCHASE", "REFUND", "PREAUTH", "VOID"]
POS_ENTRY_MODES = ["CHIP", "CHIP", "CONTACTLESS", "CONTACTLESS", "SWIPE", "ECOM", "MANUAL"]

# Card network test/sandbox BIN prefixes -- these are the same style of
# publicly-documented, non-issued ranges used by processor sandboxes
# (e.g. Stripe/Braintree test cards). Marked TEST in bin_ranges.json.
NETWORKS = [
    # name, prefix, total_length, cvv_length
    ("VISA", "400000", 16, 3),
    ("VISA", "424242", 16, 3),
    ("VISA", "411111", 16, 3),
    ("MASTERCARD", "555555", 16, 3),
    ("MASTERCARD", "510510", 16, 3),
    ("MASTERCARD", "222300", 16, 3),
    ("AMEX", "378282", 15, 4),
    ("AMEX", "371449", 15, 4),
    ("DISCOVER", "601111", 16, 3),
    ("JCB", "353011", 16, 3),
    ("DINERS", "305693", 14, 3),
]

ACQUIRER_ID = "ACQ-TESTPOC-001"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def luhn_check_digit(partial_number: str) -> str:
    digits = [int(d) for d in partial_number]
    digits.reverse()
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    check = (10 - (total % 10)) % 10
    return str(check)


def generate_pan(prefix: str, length: int) -> str:
    body_len = length - len(prefix) - 1
    body = "".join(str(random.randint(0, 9)) for _ in range(body_len))
    partial = prefix + body
    return partial + luhn_check_digit(partial)


def random_name():
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def random_expiry(after: datetime):
    year = after.year + random.randint(1, 4)
    month = random.randint(1, 12)
    return f"{month:02d}/{str(year)[2:]}"


def random_cvv(length: int) -> str:
    return "".join(str(random.randint(0, 9)) for _ in range(length))


def build_track2(pan: str, expiry_mmYY: str, service_code="201"):
    mm, yy = expiry_mmYY.split("/")
    disc = "".join(str(random.randint(0, 9)) for _ in range(8))
    return f"{pan}={yy}{mm}{service_code}{disc}"


def weighted_choice(items, weights):
    return random.choices(items, weights=weights, k=1)[0]


def gen_id(prefix, n, width=6):
    return f"{prefix}{n:0{width}d}"


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def generate_merchants(n):
    merchants = []
    for i in range(n):
        mid = gen_id("MERCH", i + 1, 6)
        mcc, mcc_desc = random.choice(MCC_TABLE)
        merchants.append({
            "merchant_id": mid,
            "legal_name": MERCHANT_NAMES[i % len(MERCHANT_NAMES)] + (f" #{i // len(MERCHANT_NAMES) + 1}" if i >= len(MERCHANT_NAMES) else ""),
            "mcc": mcc,
            "mcc_description": mcc_desc,
            "acquirer_id": ACQUIRER_ID,
            "business_type": random.choice(["SOLE_PROPRIETOR", "LLC", "CORPORATION", "PARTNERSHIP"]),
            "onboarded_date": (datetime(2022, 1, 1) + timedelta(days=random.randint(0, 900))).strftime("%Y-%m-%d"),
            "address": {
                "line1": f"{random.randint(10, 9999)} {random.choice(['Main St', 'Market St', 'Elm Ave', 'Industrial Way', 'Harbor Rd'])}",
                "city": random.choice(["Springfield", "Riverside", "Fairview", "Georgetown", "Clinton"]),
                "country": random.choice(["US", "GB", "CA", "AU", "DE"]),
                "postal_code": f"{random.randint(10000, 99999)}",
            },
        })
    return merchants


def generate_terminals(merchants, per_merchant_range=(1, 4)):
    terminals = []
    counter = 1
    for m in merchants:
        n_term = random.randint(*per_merchant_range)
        for _ in range(n_term):
            terminals.append({
                "terminal_id": gen_id("TERM", counter, 6),
                "merchant_id": m["merchant_id"],
                "terminal_type": random.choice(["POS_CHIP_PIN", "POS_CONTACTLESS", "ECOM_GATEWAY", "MOBILE_POS"]),
                "serial_number": f"SN{random.randint(10**9, 10**10 - 1)}",
                "location": m["address"]["city"],
            })
            counter += 1
    return terminals


def generate_cards(n):
    cards = []
    for i in range(n):
        network, prefix, length, cvv_len = random.choice(NETWORKS)
        pan = generate_pan(prefix, length)
        base_date = datetime(2026, 9, 5)
        expiry = random_expiry(base_date)
        cards.append({
            "card_id": gen_id("CARD", i + 1, 6),
            "pan": pan,
            "cardholder_name": random_name(),
            "expiry_date": expiry,
            "cvv": random_cvv(cvv_len),
            "track2": build_track2(pan, expiry),
            "card_network": network,
            "issuing_bin": pan[:6],
            "created_at": (base_date - timedelta(days=random.randint(30, 900))).strftime("%Y-%m-%d"),
        })
    return cards


def generate_authorizations(n, merchants, terminals, cards, days_back=30):
    terms_by_merchant = {}
    for t in terminals:
        terms_by_merchant.setdefault(t["merchant_id"], []).append(t)

    now = datetime(2026, 9, 5, 12, 0, 0)
    auths = []
    for i in range(n):
        merchant = random.choice(merchants)
        terminal = random.choice(terms_by_merchant[merchant["merchant_id"]])
        card = random.choice(cards)
        ts = now - timedelta(
            days=random.randint(0, days_back),
            hours=random.randint(0, 23),
            minutes=random.randint(0, 59),
            seconds=random.randint(0, 59),
        )
        resp_code, resp_text, resp_status = weighted_choice(RESPONSE_CODES, RESPONSE_WEIGHTS)
        currency = random.choice(CURRENCIES)
        amount_major = round(random.uniform(1.00, 850.00), 2)

        auths.append({
            "transaction_id": str(uuid.uuid4()),
            "merchant_id": merchant["merchant_id"],
            "terminal_id": terminal["terminal_id"],
            "pan": card["pan"],
            "expiry_date": card["expiry_date"],
            "card_network": card["card_network"],
            "transaction_type": random.choice(TXN_TYPES),
            "amount": f"{amount_major:.2f}",
            "currency_code_numeric": currency[0],
            "currency_code_alpha": currency[1],
            "mcc": merchant["mcc"],
            "pos_entry_mode": random.choice(POS_ENTRY_MODES),
            "auth_code": "".join(str(random.randint(0, 9)) for _ in range(6)),
            "response_code": resp_code,
            "response_text": resp_text,
            "response_status": resp_status,
            "stan": f"{random.randint(0, 999999):06d}",
            "retrieval_reference_number": "".join(str(random.randint(0, 9)) for _ in range(12)),
            "avs_result": random.choice(["Y", "N", "A", "Z", "U"]),
            "cvv_result": random.choice(["M", "N", "P", "U"]),
            "acquirer_id": ACQUIRER_ID,
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return auths


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------

def write_csv(path, rows, fieldnames):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            flat = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items() if k in fieldnames}
            w.writerow(flat)


def write_schema_sql(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    schema = """\
-- Synthetic merchant-acquiring schema for encryption POC testing.
-- pan / cvv / track2 / cardholder_name are the columns intended to be
-- protected by application-level, tokenization, or transparent
-- storage-layer encryption controls (any vendor).
-- cardholder_name, cvv and track2 live only on the cards table (the
-- card vault), not on authorizations: they are not part of the data a
-- merchant would see in an ISO 8583 authorization message/response,
-- so they are looked up via cards.pan when needed rather than
-- denormalized onto every transaction.

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
    pan                         TEXT NOT NULL,   -- sensitive (denormalized for encryption-at-rest testing)
    expiry_date                 TEXT NOT NULL,   -- sensitive
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
CREATE INDEX IF NOT EXISTS idx_auth_pan ON authorizations(pan);
CREATE INDEX IF NOT EXISTS idx_auth_timestamp ON authorizations(timestamp);
"""
    with open(path, "w") as f:
        f.write(schema)
    return schema


def write_sqlite_db(path, schema_sql, merchants, terminals, cards, auths):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.executescript(schema_sql)

    cur.executemany(
        "INSERT INTO merchants VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(m["merchant_id"], m["legal_name"], m["mcc"], m["mcc_description"], m["acquirer_id"],
          m["business_type"], m["onboarded_date"], m["address"]["line1"], m["address"]["city"],
          m["address"]["country"], m["address"]["postal_code"]) for m in merchants],
    )
    cur.executemany(
        "INSERT INTO terminals VALUES (?,?,?,?,?)",
        [(t["terminal_id"], t["merchant_id"], t["terminal_type"], t["serial_number"], t["location"]) for t in terminals],
    )
    cur.executemany(
        "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?)",
        [(c["card_id"], c["pan"], c["cardholder_name"], c["expiry_date"], c["cvv"],
          c["track2"], c["card_network"], c["issuing_bin"], c["created_at"]) for c in cards],
    )
    cur.executemany(
        "INSERT INTO authorizations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(a["transaction_id"], a["merchant_id"], a["terminal_id"], a["pan"],
          a["expiry_date"], a["card_network"], a["transaction_type"],
          a["amount"], a["currency_code_numeric"], a["currency_code_alpha"], a["mcc"], a["pos_entry_mode"],
          a["auth_code"], a["response_code"], a["response_text"], a["response_status"], a["stan"],
          a["retrieval_reference_number"], a["avs_result"], a["cvv_result"], a["acquirer_id"], a["timestamp"])
         for a in auths],
    )
    conn.commit()
    conn.close()


def write_json_files_per_auth(base_dir, auths):
    for a in auths:
        ts = datetime.strptime(a["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
        day_dir = os.path.join(base_dir, a["merchant_id"], ts.strftime("%Y-%m-%d"))
        os.makedirs(day_dir, exist_ok=True)
        path = os.path.join(day_dir, f"{a['transaction_id']}.json")
        with open(path, "w") as f:
            json.dump(a, f, indent=2)


def write_authorizations_xml(path, auths):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    root = ET.Element("Authorizations")
    for a in auths:
        txn = ET.SubElement(root, "Authorization", {"id": a["transaction_id"]})
        for k, v in a.items():
            if k == "transaction_id":
                continue
            el = ET.SubElement(txn, k)
            el.text = str(v)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def write_reference_data(ref_dir):
    os.makedirs(ref_dir, exist_ok=True)

    with open(os.path.join(ref_dir, "mcc_codes.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mcc", "description"])
        w.writerows(MCC_TABLE)

    with open(os.path.join(ref_dir, "currency_codes.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["numeric_code", "alpha_code", "minor_unit", "name"])
        w.writerows(CURRENCIES)

    with open(os.path.join(ref_dir, "response_codes.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "description", "status"])
        w.writerows(RESPONSE_CODES)

    bin_ranges = [
        {"network": n, "bin_prefix": p, "pan_length": l, "cvv_length": c, "range_type": "TEST_SANDBOX_ONLY"}
        for (n, p, l, c) in NETWORKS
    ]
    with open(os.path.join(ref_dir, "bin_ranges.json"), "w") as f:
        json.dump(bin_ranges, f, indent=2)

    card_networks = {
        "VISA": {"pan_lengths": [16], "cvv_field": "CVV2", "cvv_length": 3, "luhn": True},
        "MASTERCARD": {"pan_lengths": [16], "cvv_field": "CVC2", "cvv_length": 3, "luhn": True},
        "AMEX": {"pan_lengths": [15], "cvv_field": "CID", "cvv_length": 4, "luhn": True},
        "DISCOVER": {"pan_lengths": [16], "cvv_field": "CID", "cvv_length": 3, "luhn": True},
        "JCB": {"pan_lengths": [16], "cvv_field": "CAV2", "cvv_length": 3, "luhn": True},
        "DINERS": {"pan_lengths": [14], "cvv_field": "CVV", "cvv_length": 3, "luhn": True},
    }
    with open(os.path.join(ref_dir, "card_networks.json"), "w") as f:
        json.dump(card_networks, f, indent=2)


def write_merchant_reference(ref_dir, merchants, terminals):
    terms_by_merchant = {}
    for t in terminals:
        terms_by_merchant.setdefault(t["merchant_id"], []).append(t)

    for m in merchants:
        mdir = os.path.join(ref_dir, "merchants", m["merchant_id"])
        os.makedirs(mdir, exist_ok=True)
        with open(os.path.join(mdir, "profile.json"), "w") as f:
            json.dump(m, f, indent=2)
        with open(os.path.join(mdir, "terminals.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["terminal_id", "merchant_id", "terminal_type", "serial_number", "location"])
            w.writeheader()
            for t in terms_by_merchant.get(m["merchant_id"], []):
                w.writerow(t)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data", help="output base directory")
    ap.add_argument("--merchants", type=int, default=250)
    ap.add_argument("--cards", type=int, default=20000)
    ap.add_argument("--authorizations", type=int, default=200000)
    ap.add_argument("--days-back", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)

    merchants = generate_merchants(args.merchants)
    terminals = generate_terminals(merchants)
    cards = generate_cards(args.cards)
    auths = generate_authorizations(args.authorizations, merchants, terminals, cards, args.days_back)

    out = args.out

    # --- database ingestion form ---
    schema_sql = write_schema_sql(os.path.join(out, "db", "schema.sql"))
    write_csv(os.path.join(out, "db", "csv", "merchants.csv"), [
        {**m, "address_line1": m["address"]["line1"], "address_city": m["address"]["city"],
         "address_country": m["address"]["country"], "address_postal": m["address"]["postal_code"]}
        for m in merchants
    ], ["merchant_id", "legal_name", "mcc", "mcc_description", "acquirer_id", "business_type",
        "onboarded_date", "address_line1", "address_city", "address_country", "address_postal"])
    write_csv(os.path.join(out, "db", "csv", "terminals.csv"), terminals,
              ["terminal_id", "merchant_id", "terminal_type", "serial_number", "location"])
    write_csv(os.path.join(out, "db", "csv", "cards.csv"), cards,
              ["card_id", "pan", "cardholder_name", "expiry_date", "cvv", "track2",
               "card_network", "issuing_bin", "created_at"])
    write_csv(os.path.join(out, "db", "csv", "authorizations.csv"), auths,
              ["transaction_id", "merchant_id", "terminal_id", "pan",
               "expiry_date", "card_network", "transaction_type",
               "amount", "currency_code_numeric", "currency_code_alpha", "mcc", "pos_entry_mode",
               "auth_code", "response_code", "response_text", "response_status", "stan",
               "retrieval_reference_number", "avs_result", "cvv_result", "acquirer_id", "timestamp"])
    write_sqlite_db(os.path.join(out, "db", "payments.db"), schema_sql, merchants, terminals, cards, auths)

    # --- structured files (same data) ---
    write_json_files_per_auth(os.path.join(out, "structured", "json", "authorizations"), auths)
    with open(os.path.join(out, "structured", "json", "authorizations_all.json"), "w") as f:
        json.dump(auths, f, indent=2)
    write_authorizations_xml(os.path.join(out, "structured", "xml", "authorizations.xml"), auths)

    # --- generic reference data ---
    write_reference_data(os.path.join(out, "reference"))
    write_merchant_reference(os.path.join(out, "reference"), merchants, terminals)

    print(f"Merchants:      {len(merchants)}")
    print(f"Terminals:      {len(terminals)}")
    print(f"Cards:          {len(cards)}")
    print(f"Authorizations: {len(auths)}")
    print(f"Written under:  {os.path.abspath(out)}")


if __name__ == "__main__":
    main()
