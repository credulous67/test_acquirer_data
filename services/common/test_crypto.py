from services.common.crypto import (
    generate_aes_key,
    iso4_decode_pin_block,
    iso4_encode_pin_block,
    key_from_hex,
    key_to_hex,
    translate_pin_block,
)

PAN = "4242424242424242"


def test_pin_block_round_trip():
    key = generate_aes_key()
    for pin in ("1234", "0000", "999999", "123456789012"):
        block = iso4_encode_pin_block(pin, PAN, key)
        assert len(block) == 16
        assert iso4_decode_pin_block(block, PAN, key) == pin


def test_pin_block_is_randomised_each_time():
    key = generate_aes_key()
    a = iso4_encode_pin_block("1234", PAN, key)
    b = iso4_encode_pin_block("1234", PAN, key)
    assert a != b  # random padding must differ block-to-block


def test_wrong_key_does_not_recover_pin():
    key1 = generate_aes_key()
    key2 = generate_aes_key()
    block = iso4_encode_pin_block("1234", PAN, key1)
    try:
        recovered = iso4_decode_pin_block(block, PAN, key2)
    except ValueError:
        recovered = None  # rejected as not a valid ISO-4 block -- also an acceptable outcome
    assert recovered != "1234"


def test_translate_pin_block_between_zones():
    terminal_zpk = generate_aes_key()
    issuer_zpk = generate_aes_key()
    block = iso4_encode_pin_block("4321", PAN, terminal_zpk)

    translated = translate_pin_block(block, PAN, terminal_zpk, issuer_zpk)

    assert translated != block
    assert iso4_decode_pin_block(translated, PAN, issuer_zpk) == "4321"
    # the terminal key must no longer be able to decode the translated block
    try:
        recovered = iso4_decode_pin_block(translated, PAN, terminal_zpk)
    except ValueError:
        recovered = None
    assert recovered != "4321"


def test_key_hex_round_trip():
    key = generate_aes_key()
    assert key_from_hex(key_to_hex(key)) == key
