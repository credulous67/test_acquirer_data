from services.common import iso8583


def test_pack_unpack_round_trip_without_pin():
    values = {
        "pan": "4242424242424242",
        "processing_code": "000000",
        "amount_minor_units": "000000012345",
        "transmission_datetime": "0905120000",
        "stan": "000123",
        "expiry_date": "2809",
        "mcc": "5411",
        "pos_entry_mode": "05",
        "acquirer_id": "ACQ-TESTPOC-001",
        "retrieval_reference_number": "123456789012",
        "terminal_id": "TERM001",
        "merchant_id": "MERCH000001",
        "currency_code_numeric": "840",
        "transaction_id": "11111111-1111-1111-1111-111111111111",
        "extra_json": {"card_network": "VISA", "pin_present": False},
    }
    data = iso8583.pack(iso8583.MTI_AUTH_REQUEST, values)
    mti, decoded = iso8583.unpack(data)

    assert mti == iso8583.MTI_AUTH_REQUEST
    assert decoded["pan"] == values["pan"]
    assert decoded["merchant_id"] == values["merchant_id"]
    assert decoded["extra_json"] == values["extra_json"]
    assert "pin_block" not in decoded


def test_pack_unpack_with_pin_block():
    pin_block = bytes(range(16))
    data = iso8583.pack(iso8583.MTI_AUTH_REQUEST, {"pan": "4242424242424242", "pin_block": pin_block})
    _mti, decoded = iso8583.unpack(data)
    assert decoded["pin_block"] == pin_block


def test_response_fields():
    data = iso8583.pack(iso8583.MTI_AUTH_RESPONSE, {
        "transaction_id": "abc",
        "response_code": "00",
        "auth_code": "123456",
    })
    _mti, decoded = iso8583.unpack(data)
    assert decoded["response_code"] == "00"
    assert decoded["auth_code"] == "123456"
