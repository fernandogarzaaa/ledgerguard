import pytest

from app.incident.invoices import parse_claimed_amount, parse_invoice_ids
from app.incident.policy import make_request_id
from tests.conftest import TEXT


@pytest.mark.parametrize(
    "key,needle",
    [
        ("IN_FLIGHT", "on its way"),
        ("PAID_CONFIRMED", "paid in full"),
        ("RETURNED", "returned to us by your bank"),
        ("BAD_BENEFICIARY", "verify your bank details"),
        ("TRULY_UNPAID", "had not been paid"),
        ("DUPLICATE_RISK", "once, not twice"),
        ("FAILED", "replacement payment"),
        ("COMPLIANCE", "looking into this"),
    ],
)
async def test_supplier_message_per_case(svc, key, needle):
    inc = await svc.investigate(TEXT[key])
    msg = inc.supplier_message
    assert needle in msg
    assert inc.bundle.invoice.invoice_id in msg
    assert "••••" not in msg  # no account numbers go to the supplier


async def test_in_flight_message_cites_reference(svc):
    inc = await svc.investigate(TEXT["IN_FLIGHT"])
    assert "P261008-KST1042" in inc.supplier_message


async def test_report_sections(svc):
    inc = await svc.investigate(TEXT["DUPLICATE_RISK"])
    r = inc.report
    for section in (
        "## Timeline (UTC)",
        "## Decision",
        "### Evidence",
        "## Approval",
        "## Execution",
    ):
        assert section in r
    assert "Pending human approval" in r


def test_request_id_rules():
    a = make_request_id("INV-2001", "REPLACE_PAYOUT", "trf", "2", "4000.00")
    b = make_request_id("INV-2001", "REPLACE_PAYOUT", "trf", "2", "4000.00")
    c = make_request_id("INV-2001", "REPLACE_PAYOUT", "trf", "3", "4000.00")
    assert a == b != c
    assert 10 <= len(a) <= 50


async def test_swift_fee_in_cost_note(svc):
    inc = await svc.investigate(TEXT["FAILED"])
    assert "12.85" in inc.action.cost_note
    assert "second fee" in inc.action.cost_note


def test_text_parsing():
    assert parse_invoice_ids("re: inv 1042 and INV-1043, inv_1042") == ["INV-1042", "INV-1043"]
    assert parse_claimed_amount("for USD 3,200.50 today") == (
        __import__("decimal").Decimal("3200.50"),
        "USD",
    )
    assert parse_claimed_amount("€2,750 overdue")[1] == "EUR"
    assert parse_claimed_amount("nothing") is None


async def test_invoice_book_from_file(tmp_path):
    import json

    from app.incident.invoices import InvoiceBook

    p = tmp_path / "book.json"
    p.write_text(
        json.dumps(
            {
                "invoices": [
                    {
                        "invoice_id": "inv-7",
                        "amount": "10",
                        "currency": "usd",
                        "supplier": "Acme Co",
                        "beneficiary_id": "b",
                    }
                ]
            }
        )
    )
    book = InvoiceBook.from_file(p)
    inv = book.get("INV-7")
    assert inv.currency == "USD" and str(inv.amount) == "10"
    assert book.find_by_supplier("acme says hi")[0].invoice_id == "INV-7"
