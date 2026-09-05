#!/usr/bin/env python3
"""
Seed/reference data generator for the live merchant-acquirer authorization
simulation (see README.md and podman-compose.yml).

This script does NOT populate a database and does NOT decide any
authorization outcomes -- both of those now happen live, at run time, in
the gateway and issuer-simulator services. All this script produces is:

  - static reference data merchants/terminals/cards need (merchants,
    terminals, the card "vault", generic MCC/currency/response-code/BIN
    reference tables) -- data/seed/*.json and data/reference/*
  - a pool of authorization *request* templates the merchant-simulator
    replays as live traffic -- data/seed/authorizations.json
  - the AES keys (ZPKs) used to encipher PIN blocks in transit --
    data/keys/keys.json
  - a TEST-ONLY oracle of what each simulated cardholder types at the PIN
    pad -- data/seed/customer_pins.json

ALL data produced by this script is fake:
  - PANs are built from publicly-documented test/sandbox BIN prefixes
    (the same ranges Visa, Mastercard, Amex and processors like Stripe
    publish for sandbox use) with randomised trailing digits and a
    correctly computed Luhn check digit. They are structurally valid
    but are NOT real, issued account numbers.
  - Names, addresses, merchants, CVVs, PINs and track data are randomly
    generated and do not correspond to real people or businesses.

Two outputs deliberately model something that would be a serious security
violation in a real system, and both are called out again in README.md:
  - data/keys/keys.json holds AES ZPKs in the clear. A real ZPK only ever
    exists inside an HSM boundary.
  - data/seed/customer_pins.json is a test oracle standing in for "what
    the fake cardholder types at the PIN pad" (keyed by PAN, the only
    thing a terminal actually has). A real merchant/terminal has no such
    file.
"""
import argparse
import json
import os
import random
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.common import reference
from services.common.crypto import generate_aes_key, iso4_encode_pin_block, key_to_hex

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


def random_pin(length: int = 4) -> str:
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
        mcc, mcc_desc = random.choice(reference.MCC_TABLE)
        merchants.append({
            "merchant_id": mid,
            "legal_name": MERCHANT_NAMES[i % len(MERCHANT_NAMES)] + (f" #{i // len(MERCHANT_NAMES) + 1}" if i >= len(MERCHANT_NAMES) else ""),
            "mcc": mcc,
            "mcc_description": mcc_desc,
            "acquirer_id": reference.ACQUIRER_ID,
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


def generate_cards_and_pins(n, issuer_zpks):
    """Returns (cards, customer_pins). cards never carries a plaintext
    PIN -- only an ISO-4 block, enciphered under the owning issuer's ZPK,
    the way an issuer's own PIN store would hold it. customer_pins is a
    separate, clearly test-only oracle (see module docstring)."""
    cards = []
    customer_pins = {}
    for i in range(n):
        network, prefix, length, cvv_len = random.choice(reference.NETWORKS)
        pan = generate_pan(prefix, length)
        base_date = datetime(2026, 9, 5)
        expiry = random_expiry(base_date)
        card_id = gen_id("CARD", i + 1, 6)
        issuer_id = reference.issuer_id_for_network(network)
        pin = random_pin()

        pin_block = iso4_encode_pin_block(pin, pan, issuer_zpks[issuer_id])

        cards.append({
            "card_id": card_id,
            "pan": pan,
            "cardholder_name": random_name(),
            "expiry_date": expiry,
            "cvv": random_cvv(cvv_len),
            "track2": build_track2(pan, expiry),
            "card_network": network,
            "issuer_id": issuer_id,
            "issuing_bin": pan[:6],
            "pin_block_at_rest_hex": pin_block.hex(),
            "created_at": (base_date - timedelta(days=random.randint(30, 900))).strftime("%Y-%m-%d"),
        })
        # keyed by PAN, not card_id: a real terminal only ever has the
        # PAN it just read, never an acquirer/issuer-internal surrogate key
        customer_pins[pan] = pin
    return cards, customer_pins


def pin_present_for_entry_mode(pos_entry_mode: str) -> bool:
    p = reference.PIN_PRESENT_PROBABILITY.get(pos_entry_mode, 0.0)
    return random.random() < p


def generate_authorizations(n, merchants, terminals, cards, days_back=30):
    """Request-only records: no response fields, because the response is
    now decided live by the issuer-simulator, not pre-baked here."""
    terms_by_merchant = {}
    for t in terminals:
        terms_by_merchant.setdefault(t["merchant_id"], []).append(t)

    now = datetime(2026, 9, 5, 12, 0, 0)
    auths = []
    for _ in range(n):
        merchant = random.choice(merchants)
        terminal = random.choice(terms_by_merchant[merchant["merchant_id"]])
        card = random.choice(cards)
        ts = now - timedelta(
            days=random.randint(0, days_back),
            hours=random.randint(0, 23),
            minutes=random.randint(0, 59),
            seconds=random.randint(0, 59),
        )
        currency = random.choice(reference.CURRENCIES)
        amount_major = round(random.uniform(1.00, 850.00), 2)
        pos_entry_mode = random.choice(reference.POS_ENTRY_MODES)

        auths.append({
            "transaction_id": str(uuid.uuid4()),
            "merchant_id": merchant["merchant_id"],
            "terminal_id": terminal["terminal_id"],
            "pan": card["pan"],
            "expiry_date": card["expiry_date"],
            "card_network": card["card_network"],
            "transaction_type": random.choice(reference.TXN_TYPES),
            "amount": f"{amount_major:.2f}",
            "currency_code_numeric": currency[0],
            "currency_code_alpha": currency[1],
            "mcc": merchant["mcc"],
            "pos_entry_mode": pos_entry_mode,
            "pin_present": pin_present_for_entry_mode(pos_entry_mode),
            "stan": f"{random.randint(0, 999999):06d}",
            "retrieval_reference_number": "".join(str(random.randint(0, 9)) for _ in range(12)),
            "acquirer_id": reference.ACQUIRER_ID,
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return auths


def generate_keys(terminals):
    terminal_zpks = {t["terminal_id"]: generate_aes_key() for t in terminals}
    issuer_zpks = {f"ISSUER-{name}": generate_aes_key() for name in reference.NETWORK_NAMES}
    return terminal_zpks, issuer_zpks


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------

def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def write_reference_data(ref_dir):
    os.makedirs(ref_dir, exist_ok=True)

    write_json(os.path.join(ref_dir, "mcc_codes.json"),
               [{"mcc": m, "description": d} for m, d in reference.MCC_TABLE])
    write_json(os.path.join(ref_dir, "currency_codes.json"),
               [{"numeric_code": n, "alpha_code": a, "minor_unit": u, "name": name} for n, a, u, name in reference.CURRENCIES])
    write_json(os.path.join(ref_dir, "response_codes.json"),
               [{"code": c, "description": d, "status": s} for c, d, s in reference.RESPONSE_CODES])

    bin_ranges = [
        {"network": n, "bin_prefix": p, "pan_length": l, "cvv_length": c, "range_type": "TEST_SANDBOX_ONLY"}
        for (n, p, l, c) in reference.NETWORKS
    ]
    write_json(os.path.join(ref_dir, "bin_ranges.json"), bin_ranges)

    card_networks = {
        "VISA": {"pan_lengths": [16], "cvv_field": "CVV2", "cvv_length": 3, "luhn": True},
        "MASTERCARD": {"pan_lengths": [16], "cvv_field": "CVC2", "cvv_length": 3, "luhn": True},
        "AMEX": {"pan_lengths": [15], "cvv_field": "CID", "cvv_length": 4, "luhn": True},
        "DISCOVER": {"pan_lengths": [16], "cvv_field": "CID", "cvv_length": 3, "luhn": True},
        "JCB": {"pan_lengths": [16], "cvv_field": "CAV2", "cvv_length": 3, "luhn": True},
        "DINERS": {"pan_lengths": [14], "cvv_field": "CVV", "cvv_length": 3, "luhn": True},
    }
    write_json(os.path.join(ref_dir, "card_networks.json"), card_networks)


def write_merchant_reference(ref_dir, merchants, terminals):
    terms_by_merchant = {}
    for t in terminals:
        terms_by_merchant.setdefault(t["merchant_id"], []).append(t)

    for m in merchants:
        mdir = os.path.join(ref_dir, "merchants", m["merchant_id"])
        write_json(os.path.join(mdir, "profile.json"), m)
        write_json(os.path.join(mdir, "terminals.json"), terms_by_merchant.get(m["merchant_id"], []))


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
    ap.add_argument("--if-missing", action="store_true",
                     help="skip generation entirely if data/seed/authorizations.json already "
                          "exists. AES keys are generated with the secrets module, not the "
                          "--seed'd random one, so they differ on every run; if a compose/orchestration "
                          "tool re-triggers this one-shot generator more than once (some do, against "
                          "a service meant to run exactly once), a second run would silently replace "
                          "cards.json's at-rest PIN blocks and keys.json's ZPKs with a fresh, "
                          "inconsistent pair out from under services that already read the first set. "
                          "This flag is what the seed-generator container passes by default.")
    args = ap.parse_args()

    marker = os.path.join(args.out, "seed", "authorizations.json")
    if args.if_missing and os.path.exists(marker):
        print(f"{marker} already exists and --if-missing was given; skipping generation.")
        return

    random.seed(args.seed)

    merchants = generate_merchants(args.merchants)
    terminals = generate_terminals(merchants)
    terminal_zpks, issuer_zpks = generate_keys(terminals)
    cards, customer_pins = generate_cards_and_pins(args.cards, issuer_zpks)
    auths = generate_authorizations(args.authorizations, merchants, terminals, cards, args.days_back)

    out = args.out

    # --- seed data for the live simulation ---
    write_json(os.path.join(out, "seed", "merchants.json"), merchants)
    write_json(os.path.join(out, "seed", "terminals.json"), terminals)
    write_json(os.path.join(out, "seed", "cards.json"), cards)
    write_json(os.path.join(out, "seed", "authorizations.json"), auths)
    write_json(os.path.join(out, "seed", "customer_pins.json"), customer_pins)

    # --- keys (TEST-ONLY plaintext custody -- see module docstring) ---
    write_json(os.path.join(out, "keys", "keys.json"), {
        "terminals": {tid: key_to_hex(k) for tid, k in terminal_zpks.items()},
        "issuers": {iid: key_to_hex(k) for iid, k in issuer_zpks.items()},
    })

    # --- generic, non-cardholder reference data ---
    write_reference_data(os.path.join(out, "reference"))
    write_merchant_reference(os.path.join(out, "reference"), merchants, terminals)

    print(f"Merchants:      {len(merchants)}")
    print(f"Terminals:      {len(terminals)}")
    print(f"Cards:          {len(cards)}")
    print(f"Authorizations: {len(auths)} (request templates, no responses)")
    print(f"Written under:  {os.path.abspath(out)}")


if __name__ == "__main__":
    main()
