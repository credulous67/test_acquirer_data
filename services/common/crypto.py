"""
AES key handling and ISO 9564-1 Format 4 (AES) PIN block construction.

This module implements the *general structure* of an ISO-4 PIN block --
control nibble + PIN-length + PIN digits + random filler for the PIN
field, XORed against a PAN-derived field, put through AES twice -- so
that PIN blocks in this simulation behave the way real ones do: they are
reversible only with the correct key and PAN, and re-enciphering under a
different key (a "translation", see translate_pin_block) always produces
a different block for the same PIN. It is written to be internally
consistent end-to-end across this project's own encode/decode/translate
calls; it is not a certified, byte-for-byte implementation of the ISO
9564-1 standard and must never be used outside this test/POC context.

Key custody note: a real ZPK never exists outside an HSM boundary, and
"translating" a PIN block between zones is an HSM operation performed
without the PIN ever being visible in host software. Here it's a plain
Python function because this whole project is a software simulation
harness, not a certified cryptographic device -- see README.md for the
full list of places this project deliberately cuts that corner for the
sake of being runnable without real HSM hardware.
"""
import secrets

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

AES_KEY_SIZE = 16   # AES-128
BLOCK_SIZE = 16      # one AES block


def generate_aes_key() -> bytes:
    return secrets.token_bytes(AES_KEY_SIZE)


def key_to_hex(key: bytes) -> str:
    return key.hex()


def key_from_hex(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


def _aes_ecb_encrypt_block(key: bytes, block: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(block) + encryptor.finalize()


def _aes_ecb_decrypt_block(key: bytes, block: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(block) + decryptor.finalize()


def _nibbles_to_bytes(nibbles) -> bytes:
    return bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles), 2))


def _bytes_to_nibbles(data: bytes):
    nibbles = []
    for b in data:
        nibbles.append((b >> 4) & 0xF)
        nibbles.append(b & 0xF)
    return nibbles


def _pin_field(pin: str) -> bytes:
    if not pin.isdigit() or not (4 <= len(pin) <= 12):
        raise ValueError("PIN must be 4-12 digits")
    nibbles = [0x4, len(pin)] + [int(d) for d in pin] + [0xA]
    while len(nibbles) < 32:
        nibbles.append(secrets.randbelow(16))
    return _nibbles_to_bytes(nibbles)


def _pan_field(pan: str) -> bytes:
    # rightmost 12 digits of the account number, excluding the Luhn
    # check digit -- mirrors the account-number field used by the real
    # standard's PAN field, without needing to reproduce its exact
    # length-encoding bits (unnecessary here since the PAN is always
    # supplied independently at decode time, never recovered from this
    # field).
    account = pan[:-1][-12:].rjust(12, "0")
    nibbles = [0x0] + [int(d) for d in account]
    while len(nibbles) < 32:
        nibbles.append(0x0)
    return _nibbles_to_bytes(nibbles)


def iso4_encode_pin_block(pin: str, pan: str, key: bytes) -> bytes:
    pin_field = _pin_field(pin)
    pan_field = _pan_field(pan)
    intermediate = _aes_ecb_encrypt_block(key, pin_field)
    whitened = bytes(a ^ b for a, b in zip(intermediate, pan_field))
    return _aes_ecb_encrypt_block(key, whitened)


def iso4_decode_pin_block(block: bytes, pan: str, key: bytes) -> str:
    pan_field = _pan_field(pan)
    whitened = _aes_ecb_decrypt_block(key, block)
    intermediate = bytes(a ^ b for a, b in zip(whitened, pan_field))
    pin_field = _aes_ecb_decrypt_block(key, intermediate)

    nibbles = _bytes_to_nibbles(pin_field)
    control, length = nibbles[0], nibbles[1]
    if control != 0x4:
        raise ValueError("not an ISO-4 PIN block (unexpected control nibble)")
    if not (4 <= length <= 12):
        raise ValueError("invalid PIN length recovered from block")
    return "".join(str(d) for d in nibbles[2:2 + length])


def translate_pin_block(block: bytes, pan: str, key_in: bytes, key_out: bytes) -> bytes:
    """Decrypt under key_in, re-encipher (with a fresh random pad) under
    key_out. This is what the acquirer gateway does to move a PIN block
    from a terminal's ZPK to the destination issuer's ZPK -- in a real
    system this "PIN translate" operation happens inside an HSM, which
    never releases the clear PIN outside its boundary."""
    pin = iso4_decode_pin_block(block, pan, key_in)
    return iso4_encode_pin_block(pin, pan, key_out)
