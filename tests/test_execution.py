"""End-to-end: approve -> execute against the mock sandbox, and every way to pay twice."""

from decimal import Decimal

from app.airwallex.client import AirwallexError
from app.incident.guard import DuplicatePayoutGuard
from app.incident.models import ActionKind, CaseClass
from tests.conftest import TEXT, approve_and_execute


def transfers_for(mock, invoice):
    return [
        t for t in mock.transfers.values() if (t.get("metadata") or {}).get("invoice_id") == invoice
    ]


async def test_execute_requires_approval(svc):
    inc = await svc.investigate(TEXT["FAILED"])
    r = await svc.execute(inc.id, inc.approval.approval_id, "not-a-token")
    assert r["outcome"] == "refused"
    assert "not approved" in r["message"]


async def test_replace_failed_payout_with_new_request_id(svc, mock):
    before = mock.balances["EUR"]
    inc = await svc.investigate(TEXT["FAILED"])
    _, r = await approve_and_execute(svc, inc.id)
    assert r["outcome"] == "transfer_created"
    new = r["transfer"]
    assert new["request_id"] == inc.action.request_id
    assert new["request_id"] != "lg-seed-inv2001-c9d0e1f2"
    assert new["status"] == "SCHEDULED"
    assert mock.balances["EUR"] == before - Decimal("4000.00") - Decimal("12.85")
    # Original stays CANCELLED and is recorded in the report.
    assert mock.transfers["trf_2001a"]["status"] == "CANCELLED"
    assert "Original transfer" in svc.get(inc.id).report
    assert new["short_reference_id"] in svc.get(inc.id).supplier_message


async def test_replay_of_same_approval_refused_and_no_second_transfer(svc, mock):
    inc = await svc.investigate(TEXT["FAILED"])
    appr, _ = await approve_and_execute(svc, inc.id)
    r2 = await svc.execute(inc.id, appr["approval_id"], appr["approval_token"])
    assert r2["outcome"] == "refused"
    assert len(transfers_for(mock, "INV-2001")) == 2  # original + one replacement
    assert svc.get(inc.id).execution["outcome"] == "transfer_created"  # not overwritten


async def test_reinvestigating_after_replacement_waits(svc):
    inc = await svc.investigate(TEXT["FAILED"])
    await approve_and_execute(svc, inc.id)
    again = await svc.investigate(TEXT["FAILED"])
    assert again.classification.case is CaseClass.IN_FLIGHT
    assert again.action.kind is ActionKind.WAIT_AND_MONITOR


async def test_two_parallel_incidents_cannot_both_pay(svc, mock):
    a = await svc.investigate(TEXT["TRULY_UNPAID"])
    b = await svc.investigate(TEXT["TRULY_UNPAID"] + " (second report)")
    assert a.action.request_id == b.action.request_id  # deterministic
    _, ra = await approve_and_execute(svc, a.id, "Alice")
    _, rb = await approve_and_execute(svc, b.id, "Bob")
    assert ra["outcome"] == "transfer_created"
    assert rb["outcome"] == "refused"
    assert len(transfers_for(mock, "INV-4100")) == 1


async def test_guard_blocks_when_airwallex_list_lags(svc, mock):
    mock.set_balance("EUR", "50000")  # keep the funding choice identical for both
    a = await svc.investigate(TEXT["TRULY_UNPAID"])
    b = await svc.investigate(TEXT["TRULY_UNPAID"])
    _, ra = await approve_and_execute(svc, a.id, "Alice")
    mock.hide_from_list.add(ra["transfer"]["id"])  # Airwallex has not listed it yet
    _, rb = await approve_and_execute(svc, b.id, "Bob")
    assert rb["outcome"] == "refused"
    assert "already executed" in rb["message"] or "locked" in rb["message"]
    assert len(transfers_for(mock, "INV-4100")) == 1


async def test_airwallex_idempotency_is_last_line_of_defence(svc, mock, settings):
    mock.set_balance("EUR", "50000")
    a = await svc.investigate(TEXT["TRULY_UNPAID"])
    b = await svc.investigate(TEXT["TRULY_UNPAID"])
    _, ra = await approve_and_execute(svc, a.id, "Alice")
    mock.hide_from_list.add(ra["transfer"]["id"])
    svc.guard = DuplicatePayoutGuard()  # local ledger lost (e.g. disk wiped)
    _, rb = await approve_and_execute(svc, b.id, "Bob")
    assert rb["outcome"] == "idempotent_replay"
    assert rb["transfer"]["id"] == ra["transfer"]["id"]
    assert len(transfers_for(mock, "INV-4100")) == 1


async def test_outage_after_commit_does_not_double_pay(svc, mock):
    inc = await svc.investigate(TEXT["TRULY_UNPAID"])
    mock.fail_next_create_after_commit = True
    _, r = await approve_and_execute(svc, inc.id)
    assert r["outcome"] == "idempotent_replay"
    assert len(transfers_for(mock, "INV-4100")) == 1
    assert svc.guard.lock_for("INV-4100").state == "live"


async def test_balance_change_after_approval_voids_it(svc, mock):
    inc = await svc.investigate(TEXT["TRULY_UNPAID"])
    mock.set_balance("EUR", "1")  # drained between approval and execution
    _, r = await approve_and_execute(svc, inc.id)
    assert r["outcome"] == "refused" and "changed" in r["message"]
    assert svc.get(inc.id).action.source_currency == "USD"  # re-proposed from fresh balances
    assert not transfers_for(mock, "INV-4100")


async def test_clean_airwallex_rejection_frees_lock(svc, mock, monkeypatch):
    inc = await svc.investigate(TEXT["TRULY_UNPAID"])

    async def reject(payload):
        raise AirwallexError(400, "balance_insufficient", "race with another debit")

    monkeypatch.setattr(svc.client, "create_transfer", reject)
    _, r = await approve_and_execute(svc, inc.id)
    assert r["outcome"] == "refused"
    assert svc.guard.lock_for("INV-4100").state == "released"


async def test_state_change_after_approval_voids_it(svc, mock):
    inc = await svc.investigate(TEXT["IN_FLIGHT"])
    appr = svc.approve(inc.id, "Fernando")
    await svc.client.simulate_transition("trf_1042a", "PAID")
    r = await svc.execute(inc.id, appr["approval_id"], appr["approval_token"])
    assert r["outcome"] == "refused"
    assert "changed" in r["message"]
    fresh = svc.get(inc.id)
    assert fresh.status == "stale"
    assert fresh.action.kind is ActionKind.SEND_PROOF_OF_PAYMENT
    assert fresh.approval.approval_id != appr["approval_id"]


async def test_cancel_duplicate_keeps_paid_original(svc, mock):
    before = mock.balances["USD"]
    inc = await svc.investigate(TEXT["DUPLICATE_RISK"])
    assert inc.action.target_transfer_id == "trf_3007b"
    _, r = await approve_and_execute(svc, inc.id)
    assert r["outcome"] == "duplicate_cancelled"
    assert mock.transfers["trf_3007b"]["status"] == "CANCELLED"
    assert mock.transfers["trf_3007a"]["status"] == "PAID"
    assert mock.balances["USD"] == before + Decimal("5600")


async def test_cancel_refused_if_duplicate_already_left(svc, mock):
    inc = await svc.investigate(TEXT["DUPLICATE_RISK"])
    appr = svc.approve(inc.id, "Fernando")
    await svc.client.simulate_transition("trf_3007b", "PROCESSING")
    await svc.client.simulate_transition("trf_3007b", "SENT")
    r = await svc.execute(inc.id, appr["approval_id"], appr["approval_token"])
    assert r["outcome"] == "refused"
    assert mock.transfers["trf_3007b"]["status"] == "SENT"


async def test_pay_truly_unpaid_with_fx(svc, mock):
    mock.set_balance("EUR", "0")
    usd_before = mock.balances["USD"]
    inc = await svc.investigate(TEXT["TRULY_UNPAID"])
    _, r = await approve_and_execute(svc, inc.id)
    assert r["outcome"] == "transfer_created"
    t = mock.transfers[r["transfer"]["id"]]
    assert t["source_currency"] == "USD" and t["transfer_currency"] == "EUR"
    assert mock.balances["USD"] < usd_before


async def test_non_money_actions_record_without_touching_airwallex(svc, mock):
    for key in ("IN_FLIGHT", "RETURNED", "BAD_BENEFICIARY", "PAID_CONFIRMED", "COMPLIANCE"):
        n = len(mock.transfers)
        inc = await svc.investigate(TEXT[key])
        _, r = await approve_and_execute(svc, inc.id)
        assert r["outcome"] == "recorded", key
        assert len(mock.transfers) == n
        assert mock.create_calls == 0


async def test_replacement_fails_again_then_new_replacement_allowed(svc, mock):
    inc = await svc.investigate(TEXT["FAILED"])
    _, r = await approve_and_execute(svc, inc.id)
    tid = r["transfer"]["id"]
    for s in ("PROCESSING", "SENT"):
        await svc.client.simulate_transition(tid, s)
    await svc.client.simulate_transition(tid, "FAILED", "CHANNEL_TIMEOUT")
    again = await svc.investigate(TEXT["FAILED"])
    assert again.action.kind is ActionKind.REPLACE_PAYOUT
    assert again.action.request_id != inc.action.request_id
    _, r2 = await approve_and_execute(svc, again.id)
    assert r2["outcome"] == "transfer_created"
    live = [t for t in transfers_for(mock, "INV-2001") if t["status"] not in ("CANCELLED",)]
    assert len(live) == 1


async def test_webhook_refreshes_incident_and_dedupes(svc):
    inc = await svc.investigate(TEXT["IN_FLIGHT"])
    await svc.client.simulate_transition("trf_1042a", "PAID")
    ev = {"id": "evt_1", "name": "payout.transfer.paid", "data": {"object": {"id": "trf_1042a"}}}
    r = await svc.handle_event(ev)
    assert r["incidents"] == [inc.id]
    assert svc.get(inc.id).action.kind is ActionKind.SEND_PROOF_OF_PAYMENT
    assert (await svc.handle_event(ev))["status"] == "duplicate"
