"""
Shared reference tables for the whole simulation platform.

Single source of truth for anything that both the seed generator
(scripts/generate_data.py) and a live service (issuer-simulator in
particular) need to agree on. Importing this from two different
processes is what guarantees, for example, that the response codes the
issuer-simulator can pick at runtime are exactly the ones documented in
data/reference/response_codes.csv.
"""

# Card network test/sandbox BIN prefixes -- these are the same style of
# publicly-documented, non-issued ranges used by processor sandboxes
# (e.g. Stripe/Braintree test cards).
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

# One simulated issuer per card network. The issuer-simulator container
# hosts all of these -- there is no per-issuer process or container.
NETWORK_NAMES = sorted({n for n, *_ in NETWORKS})


def issuer_id_for_network(network: str) -> str:
    return f"ISSUER-{network}"


# Response codes an issuer can return. Weighted toward approval, like a
# real portfolio. "55" (incorrect PIN) is never chosen by the weighted
# pick below -- it is only ever returned by the issuer-simulator's
# explicit PIN-verification step, same as "54" (expired card) is only
# ever returned by its explicit expiry check.
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
    ("55", "Incorrect PIN", "DECLINED"),
    ("57", "Transaction not permitted to cardholder", "DECLINED"),
    ("58", "Transaction not permitted to terminal", "DECLINED"),
    ("61", "Exceeds withdrawal amount limit", "DECLINED"),
    ("62", "Restricted card", "DECLINED"),
    ("65", "Exceeds withdrawal frequency limit", "DECLINED"),
    ("75", "PIN tries exceeded", "DECLINED"),
    ("91", "Issuer or switch inoperative", "DECLINED"),
    ("96", "System malfunction", "DECLINED"),
]

# Codes the issuer-simulator's generic weighted pick is allowed to choose
# (i.e. everything except the ones reserved for an explicit check above).
_RESERVED = {"54", "55"}
GENERIC_RESPONSE_CODES = [c for c in RESPONSE_CODES if c[0] not in _RESERVED]
# weighted toward approval -- 85% here, with a small additional sliver of
# real-world declines from the PIN-mismatch (55) and expired-card (54)
# checks elsewhere, keeps the overall approve rate comfortably above 80%
GENERIC_RESPONSE_WEIGHTS = [85] + [15 / (len(GENERIC_RESPONSE_CODES) - 1)] * (len(GENERIC_RESPONSE_CODES) - 1)

RESPONSE_CODE_MAP = {code: (desc, status) for code, desc, status in RESPONSE_CODES}

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

TXN_TYPES = ["PURCHASE", "PURCHASE", "PURCHASE", "PURCHASE", "REFUND", "PREAUTH", "VOID"]

# card-present entry modes can carry a PIN; card-not-present ones never do
POS_ENTRY_MODES = ["CHIP", "CHIP", "CONTACTLESS", "CONTACTLESS", "SWIPE", "ECOM", "MANUAL"]
PIN_CAPABLE_ENTRY_MODES = {"CHIP", "SWIPE", "CONTACTLESS"}
PIN_PRESENT_PROBABILITY = {"CHIP": 0.5, "SWIPE": 0.5, "CONTACTLESS": 0.15}

# human-facing auth type labels, keyed by the same entry-mode names used
# throughout (iso8583.POS_ENTRY_MODE_NAMES) -- single source of truth for
# the dashboard's feed column and auth-type breakdown chart
AUTH_TYPE_LABELS = {
    "CHIP": "EMV",
    "CONTACTLESS": "Contactless",
    "SWIPE": "Magstripe",
    "ECOM": "CNP (eCom)",
    "MANUAL": "CNP (MOTO)",
}
AUTH_TYPE_ORDER = ["EMV", "Contactless", "Magstripe", "CNP (eCom)", "CNP (MOTO)"]

ACQUIRER_ID = "ACQ-TESTPOC-001"
