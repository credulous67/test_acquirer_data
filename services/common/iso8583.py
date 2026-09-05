"""
Minimal ISO 8583-flavoured message codec.

This is deliberately not a general-purpose ISO 8583 implementation -- it
only knows about the specific field subset this simulation needs (a
credit/debit authorization request/response), using the standard's real
field numbers/formats where one applies cleanly to this project, and one
private-use field (63, reserved for private use in the 1993 version of
the standard) carrying a small JSON blob for the handful of project-
specific extras (card network, transaction type, currency alpha code,
whether a PIN was entered) that don't have a clean standard field of
their own. Field 52 (PIN data) is defined here as a 16-byte binary field
to hold an ISO-4/AES PIN block; the real standard's field 52 is
traditionally a fixed 8-byte DES-sized block, so this is a project-
specific extension, not standard-conformant on that one field.

Message layout on the wire: 4-byte ASCII MTI, 8-byte primary bitmap
(fields 1-64; no secondary bitmap is ever needed here since nothing past
field 63 is used), then each present field's data in ascending field
order. TCP framing (see read_message/write_message) is a 2-byte
big-endian length prefix, the same style real ISO 8583 host-to-host
links use.
"""
import json

MTI_AUTH_REQUEST = "0100"
MTI_AUTH_RESPONSE = "0110"

BITMAP_BYTES = 8

# field_number: (name, kind, length)
#   n         fixed-length numeric, ASCII, zero-padded
#   an / ans  fixed-length (alpha)numeric, ASCII, space-padded
#   llvar_n / llvar_ans   variable length, 2-digit length prefix
#   lllvar_ans            variable length, 3-digit length prefix
#   binary    fixed-length raw bytes
FIELDS = {
    2: ("pan", "llvar_n", 19),
    3: ("processing_code", "n", 6),
    4: ("amount_minor_units", "n", 12),
    7: ("transmission_datetime", "n", 10),
    11: ("stan", "n", 6),
    14: ("expiry_date", "n", 4),
    18: ("mcc", "n", 4),
    22: ("pos_entry_mode", "n", 2),
    32: ("acquirer_id", "llvar_ans", 11),
    37: ("retrieval_reference_number", "an", 12),
    38: ("auth_code", "an", 6),
    39: ("response_code", "an", 2),
    41: ("terminal_id", "ans", 10),
    42: ("merchant_id", "ans", 15),
    49: ("currency_code_numeric", "n", 3),
    52: ("pin_block", "binary", 16),
    62: ("transaction_id", "ans", 36),
    63: ("extra_json", "lllvar_ans", 999),
}
NAME_TO_FIELD = {name: num for num, (name, _kind, _len) in FIELDS.items()}

PROCESSING_CODES = {"PURCHASE": "000000", "REFUND": "200000", "PREAUTH": "300000", "VOID": "020000"}
POS_ENTRY_MODE_CODES = {"MANUAL": "01", "CHIP": "05", "CONTACTLESS": "07", "SWIPE": "90", "ECOM": "81"}
POS_ENTRY_MODE_NAMES = {v: k for k, v in POS_ENTRY_MODE_CODES.items()}


def _encode_fixed(value, kind, length):
    if kind == "binary":
        if len(value) != length:
            raise ValueError(f"binary field expected {length} bytes, got {len(value)}")
        return value
    s = str(value)
    if len(s) > length:
        raise ValueError(f"value {value!r} too long for fixed field of length {length}")
    s = s.rjust(length, "0") if kind == "n" else s.ljust(length)
    return s.encode("ascii")


def _decode_fixed(raw, kind):
    if kind == "binary":
        return raw
    return raw.decode("ascii").strip()


def _encode_var(value, prefix_digits, as_json=False):
    b = json.dumps(value).encode("utf-8") if as_json else str(value).encode("ascii")
    limit = 10 ** prefix_digits - 1
    if len(b) > limit:
        raise ValueError(f"value too long for {prefix_digits}-digit length prefix")
    return str(len(b)).zfill(prefix_digits).encode("ascii") + b


def pack(mti: str, values: dict) -> bytes:
    """values is keyed by field *name* (see FIELDS), not field number."""
    bitmap_int = 0
    body = b""
    for num in sorted(FIELDS):
        name, kind, _length = FIELDS[num]
        if values.get(name) is None:
            continue
        bitmap_int |= 1 << (64 - num)
        val = values[name]
        if kind in ("llvar_n", "llvar_ans"):
            body += _encode_var(val, 2)
        elif kind == "lllvar_ans":
            body += _encode_var(val, 3, as_json=True)
        else:
            body += _encode_fixed(val, kind, FIELDS[num][2])
    return mti.encode("ascii") + bitmap_int.to_bytes(BITMAP_BYTES, "big") + body


def unpack(data: bytes):
    mti = data[:4].decode("ascii")
    bitmap_int = int.from_bytes(data[4:4 + BITMAP_BYTES], "big")
    pos = 4 + BITMAP_BYTES
    values = {}
    for num in sorted(FIELDS):
        if not (bitmap_int >> (64 - num)) & 1:
            continue
        name, kind, length = FIELDS[num]
        if kind in ("llvar_n", "llvar_ans"):
            ll = int(data[pos:pos + 2]); pos += 2
            raw = data[pos:pos + ll]; pos += ll
            values[name] = raw.decode("ascii")
        elif kind == "lllvar_ans":
            lll = int(data[pos:pos + 3]); pos += 3
            raw = data[pos:pos + lll]; pos += lll
            values[name] = json.loads(raw.decode("utf-8"))
        else:
            raw = data[pos:pos + length]; pos += length
            values[name] = _decode_fixed(raw, kind)
    return mti, values


async def read_message(reader):
    header = await reader.readexactly(2)
    length = int.from_bytes(header, "big")
    data = await reader.readexactly(length)
    return unpack(data)


async def write_message(writer, mti: str, values: dict):
    data = pack(mti, values)
    writer.write(len(data).to_bytes(2, "big"))
    writer.write(data)
    await writer.drain()
