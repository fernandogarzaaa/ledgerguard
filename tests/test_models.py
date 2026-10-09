from decimal import Decimal

from app.airwallex.models import Balance, Beneficiary, Conversion, Transfer, fmt, parse_ts


def test_parse_ts_airwallex_offset_and_iso():
    a = parse_ts("2017-03-22T16:08:02+1100")
    b = parse_ts("2017-03-22T05:08:02Z")
    assert a == b
    assert parse_ts("2026-10-01") is not None
    assert parse_ts("") is None
    assert parse_ts("garbage") is None
    assert parse_ts(None) is None


def test_transfer_failure_top_level_shape():
    t = Transfer.from_api(
        {
            "id": "t1",
            "status": "cancelled",
            "failure_type": "beneficiary_bank_returned",
            "failure_reason": "90802 returned",
            "transfer_amount": 10,
            "transfer_currency": "USD",
        }
    )
    assert t.status == "CANCELLED"
    assert t.failure_type == "BENEFICIARY_BANK_RETURNED"
    assert t.failure_shape == "top_level"
    assert t.is_failed and not t.is_live


def test_transfer_failure_nested_shape():
    t = Transfer.from_api(
        {
            "id": "t2",
            "status": "CANCELLED",
            "failure": {
                "code": "90502",
                "message": "TM suspended",
                "details": {"type": "TM_SUSPEND"},
            },
        }
    )
    assert t.failure_type == "TM_SUSPEND"
    assert t.failure_code == "90502"
    assert t.failure_reason == "TM suspended"
    assert t.failure_shape == "nested"


def test_transfer_states():
    for s in ("SCHEDULED", "PROCESSING", "SENT", "OVERDUE"):
        t = Transfer.from_api({"id": "x", "status": s})
        assert t.is_in_flight and t.is_live and not t.is_paid
    paid = Transfer.from_api({"id": "x", "status": "PAID"})
    assert paid.is_paid and paid.is_live
    for s in ("FAILED", "CANCELLED", "APPROVAL_REJECTED"):
        assert Transfer.from_api({"id": "x", "status": s}).is_failed


def test_transfer_invoice_from_metadata_and_amounts():
    t = Transfer.from_api(
        {
            "id": "x",
            "metadata": {"invoice_id": "INV-1"},
            "transfer_amount": 4000.0,
            "status": "PAID",
        }
    )
    assert t.invoice_id == "INV-1"
    assert t.transfer_amount == Decimal("4000.0")
    assert t.summary()["amount"].startswith("4000.00")


def test_beneficiary_masks_account_number():
    b = Beneficiary.from_api(
        {
            "id": "b1",
            "transfer_methods": ["LOCAL"],
            "beneficiary": {
                "company_name": "Acme",
                "bank_details": {
                    "account_number": "000123456789",
                    "account_routing_type1": "aba",
                    "account_routing_value1": "021000021",
                    "account_name": "Acme",
                },
            },
        }
    )
    s = b.summary()
    assert s["account"] == "••••6789"
    assert "000123456789" not in str(s)
    assert s["routing"] == "aba:021000021"


def test_conversion_and_balance_parse():
    c = Conversion.from_api(
        {
            "conversion_id": "c1",
            "buy_currency": "EUR",
            "buy_amount": 1,
            "sell_currency": "USD",
            "sell_amount": 1.1,
            "status": "settled",
        }
    )
    assert c.status == "SETTLED" and c.id == "c1"
    b = Balance.from_api({"currency": "USD", "available_amount": 12.5})
    assert b.available == Decimal("12.5")
    assert fmt(Decimal("3")) == "3.00"
