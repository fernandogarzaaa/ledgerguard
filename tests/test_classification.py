from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.incident.classifier import failure_family
from app.incident.models import ActionKind, CaseClass
from tests.conftest import TEXT

EXPECTED = {
    "IN_FLIGHT": (CaseClass.IN_FLIGHT, ActionKind.WAIT_AND_MONITOR),
    "FAILED": (CaseClass.FAILED, ActionKind.REPLACE_PAYOUT),
    "RETURNED": (CaseClass.RETURNED, ActionKind.REQUEST_BANK_CONFIRMATION),
    "BAD_BENEFICIARY": (CaseClass.BAD_BENEFICIARY, ActionKind.VERIFY_BENEFICIARY),
    "TRULY_UNPAID": (CaseClass.TRULY_UNPAID, ActionKind.PAY_INVOICE),
    "DUPLICATE_RISK": (CaseClass.DUPLICATE_RISK, ActionKind.CANCEL_DUPLICATE),
    "PAID_CONFIRMED": (CaseClass.PAID_CONFIRMED, ActionKind.SEND_PROOF_OF_PAYMENT),
    "COMPLIANCE": (CaseClass.FAILED, ActionKind.ESCALATE),
}


@pytest.mark.parametrize("key", list(EXPECTED))
async def test_each_incident_class(svc, key):
    inc = await svc.investigate(TEXT[key])
    case, kind = EXPECTED[key]
    assert inc.classification.case is case
    assert inc.action.kind is kind
    assert inc.action.evidence, "every proposal carries evidence"
    assert inc.action.cost_note and inc.action.risk_note
    assert inc.status == "awaiting_approval"
    assert inc.timeline and inc.timeline[-1].kind in {"incident", "balance"}


async def test_in_flight_explains_sent_is_not_final(svc):
    inc = await svc.investigate(TEXT["IN_FLIGHT"])
    assert "not final" in " ".join(inc.classification.reasons)
    assert inc.action.next_check_at
    assert not inc.action.moves_money


async def test_cancelled_explained_as_failure_not_human(svc):
    inc = await svc.investigate(TEXT["FAILED"])
    assert any("not a human cancel" in r for r in inc.classification.reasons)


async def test_failed_links_fx_conversion_in_timeline(svc):
    inc = await svc.investigate(TEXT["FAILED"])
    kinds = [e.kind for e in inc.timeline]
    assert "conversion" in kinds
    assert inc.bundle.conversions[0].id == "conv_usdeur_2001"


async def test_bad_beneficiary_flags_vendor_change_and_name_mismatch(svc):
    inc = await svc.investigate(TEXT["BAD_BENEFICIARY"])
    assert "recent_bank_detail_change" in inc.classification.flags
    assert "account_name_mismatch" in inc.classification.flags
    assert inc.action.risk_level == "high"
    assert any(e.kind == "vendor_master" for e in inc.timeline)


async def test_unknown_invoice_escalates(svc):
    inc = await svc.investigate("Some supplier says INV-9999 is unpaid")
    assert inc.classification.case is CaseClass.UNKNOWN_INVOICE
    assert inc.action.kind is ActionKind.ESCALATE


async def test_truly_unpaid_funds_from_usd_when_eur_short(svc, mock):
    mock.set_balance("EUR", "100")
    inc = await svc.investigate(TEXT["TRULY_UNPAID"])
    a = inc.action
    assert a.kind is ActionKind.PAY_INVOICE
    assert a.currency == "EUR" and a.source_currency == "USD"
    assert "indicative" in a.cost_note


async def test_truly_unpaid_escalates_when_no_wallet_covers_it(svc, mock):
    for cur in ("EUR", "USD", "GBP"):
        mock.set_balance(cur, "10")
    inc = await svc.investigate(TEXT["TRULY_UNPAID"])
    assert inc.action.kind is ActionKind.ESCALATE
    assert not inc.action.moves_money


async def test_decision_changes_when_balance_changes(svc, mock):
    mock.set_balance("EUR", "0")
    mock.set_balance("USD", "0")
    first = await svc.investigate(TEXT["FAILED"])
    assert first.action.kind is ActionKind.ESCALATE
    mock.set_balance("EUR", "5000")
    second = await svc.investigate(TEXT["FAILED"])
    assert second.action.kind is ActionKind.REPLACE_PAYOUT


async def test_truly_unpaid_with_recent_bank_change_is_held(svc):
    inv = svc.book.get("INV-4100")
    inv.bank_details_changed_at = datetime.now(UTC) - timedelta(hours=5)
    inc = await svc.investigate(TEXT["TRULY_UNPAID"])
    assert inc.action.kind is ActionKind.VERIFY_BENEFICIARY


async def test_overdue_in_flight_escalates(svc, mock):
    mock.transfers["trf_1042a"]["status"] = "OVERDUE"
    inc = await svc.investigate(TEXT["IN_FLIGHT"])
    assert inc.classification.case is CaseClass.IN_FLIGHT
    assert inc.action.kind is ActionKind.ESCALATE


async def test_stuck_too_long_escalates(svc, mock):
    mock.transfers["trf_1042a"]["created_at"] = "2026-01-01T00:00:00+0000"
    mock.transfers["trf_1042a"]["updated_at"] = "2026-01-01T00:00:00+0000"
    svc.settings = replace(svc.settings, lookback_days=3650)
    inc = await svc.investigate(TEXT["IN_FLIGHT"])
    assert inc.action.kind is ActionKind.ESCALATE


async def test_two_paid_payouts_escalate_for_recovery(svc, mock):
    mock.transfers["trf_3007b"]["status"] = "PAID"
    inc = await svc.investigate(TEXT["DUPLICATE_RISK"])
    assert inc.classification.case is CaseClass.DUPLICATE_RISK
    assert inc.action.kind is ActionKind.ESCALATE
    assert inc.action.risk_level == "high"


async def test_hand_keyed_payout_without_reference_is_still_found(svc, mock):
    t = mock.transfers["trf_1042a"]
    t["reference"] = "supplier payment"
    t["metadata"] = {}
    inc = await svc.investigate(TEXT["IN_FLIGHT"])
    assert inc.classification.case is CaseClass.IN_FLIGHT
    assert "transfer_without_invoice_reference" in inc.classification.flags


async def test_paid_late_return_becomes_returned(svc, mock):
    c = svc.client
    await c.simulate_transition("trf_1043a", "FAILED", "BENEFICIARY_BANK_RETURNED")
    inc = await svc.investigate(TEXT["PAID_CONFIRMED"])
    assert inc.classification.case is CaseClass.RETURNED


def test_failure_families():
    assert failure_family("INVALID_SWIFT_BIC_CODE") == "bad_beneficiary"
    assert failure_family("BENEFICIARY_BANK_RETURNED") == "returned"
    assert failure_family("CHANNEL_TIMEOUT") == "retryable"
    assert failure_family("INSUFFICIENT_FUNDS") == "retryable"
    assert failure_family("TM_SUSPENDED") == "non_retryable"
    assert failure_family("") == "non_retryable"
