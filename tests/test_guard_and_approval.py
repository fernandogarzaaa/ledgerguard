import time

import pytest

from app.airwallex.models import Transfer
from app.incident.approval import ApprovalError, ApprovalGate
from app.incident.guard import DuplicatePayoutError, DuplicatePayoutGuard


def T(tid: str, status: str, rid: str = "rid-0000000001") -> Transfer:
    return Transfer.from_api({"id": tid, "status": status, "request_id": rid})


# -- duplicate payout guard ------------------------------------------------------
def test_guard_refuses_when_live_transfer_exists():
    g = DuplicatePayoutGuard()
    for status in ("SCHEDULED", "PROCESSING", "SENT", "PAID", "OVERDUE"):
        with pytest.raises(DuplicatePayoutError, match="already has a live payout"):
            g.acquire("INV-1", "rid-new-000001", [T("a", status)])
    assert len(g.refusals) == 5


def test_guard_allows_after_only_failures_and_locks():
    g = DuplicatePayoutGuard()
    lock = g.acquire("INV-1", "rid-new-000001", [T("a", "CANCELLED")])
    assert lock.state == "pending"
    # A different request for the same invoice is refused while the lock is held,
    # even though Airwallex shows nothing live yet (list lag).
    with pytest.raises(DuplicatePayoutError, match="locked by payout request"):
        g.acquire("inv-1", "rid-other-00001", [T("a", "CANCELLED")])


def test_guard_same_request_after_execution_refused():
    g = DuplicatePayoutGuard()
    g.acquire("INV-1", "rid-new-000001", [])
    g.confirm("INV-1", "rid-new-000001", T("b", "SCHEDULED", "rid-new-000001"))
    with pytest.raises(DuplicatePayoutError, match="already executed"):
        g.acquire("INV-1", "rid-new-000001", [])


def test_guard_same_request_pending_can_retry():
    # Crash between lock and Airwallex call: the same request may retry
    # (Airwallex idempotency covers the create itself).
    g = DuplicatePayoutGuard()
    g.acquire("INV-1", "rid-new-000001", [])
    assert g.acquire("INV-1", "rid-new-000001", []).state == "pending"


def test_guard_releases_when_locked_transfer_failed():
    g = DuplicatePayoutGuard()
    g.acquire("INV-1", "rid-first-00001", [])
    g.confirm("INV-1", "rid-first-00001", T("b", "SCHEDULED", "rid-first-00001"))
    failed = T("b", "CANCELLED", "rid-first-00001")
    assert g.acquire("INV-1", "rid-second-0001", [failed]).request_id == "rid-second-0001"


def test_guard_abort_frees_lock():
    g = DuplicatePayoutGuard()
    g.acquire("INV-1", "rid-first-00001", [])
    g.abort("INV-1", "rid-first-00001", "balance_insufficient")
    assert g.acquire("INV-1", "rid-second-0001", []).state == "pending"


def test_guard_persists_across_restarts(tmp_path):
    path = tmp_path / "ledger.json"
    g1 = DuplicatePayoutGuard(str(path))
    g1.acquire("INV-1", "rid-first-00001", [])
    g1.confirm("INV-1", "rid-first-00001", T("b", "SENT", "rid-first-00001"))
    g2 = DuplicatePayoutGuard(str(path))
    assert g2.lock_for("INV-1").state == "live"
    with pytest.raises(DuplicatePayoutError):
        g2.acquire("INV-1", "rid-second-0001", [])


# -- approval gate ----------------------------------------------------------------
BIND = {"kind": "PAY_INVOICE", "amount": "10", "currency": "USD", "request_id": "r"}


def test_approval_required_and_single_use():
    gate = ApprovalGate("s")
    req = gate.request("inc", BIND)
    with pytest.raises(ApprovalError, match="not approved"):
        gate.consume(req.approval_id, "x", BIND)
    tok = gate.approve(req.approval_id, "Fernando")
    assert gate.consume(req.approval_id, tok, BIND).used
    with pytest.raises(ApprovalError, match="already used"):
        gate.consume(req.approval_id, tok, BIND)


def test_forged_token_refused():
    gate = ApprovalGate("s")
    req = gate.request("inc", BIND)
    gate.approve(req.approval_id, "Fernando")
    with pytest.raises(ApprovalError, match="invalid"):
        gate.consume(req.approval_id, "0" * 64, BIND)
    other = ApprovalGate("different-secret")
    req2 = other.request("inc", BIND)
    forged = other.approve(req2.approval_id, "Fernando")
    with pytest.raises(ApprovalError):
        gate.consume(req.approval_id, forged, BIND)


def test_binding_change_voids_approval():
    gate = ApprovalGate("s")
    req = gate.request("inc", BIND)
    tok = gate.approve(req.approval_id, "Fernando")
    with pytest.raises(ApprovalError, match="changed"):
        gate.consume(req.approval_id, tok, {**BIND, "amount": "11"})


def test_agent_cannot_approve_and_name_required():
    gate = ApprovalGate("s")
    req = gate.request("inc", BIND)
    for who in ("agent", "LLM", "ledgerguard", ""):
        with pytest.raises(ApprovalError):
            gate.approve(req.approval_id, who)


def test_expired_approval(monkeypatch):
    gate = ApprovalGate("s", ttl_s=1)
    req = gate.request("inc", BIND)
    tok = gate.approve(req.approval_id, "Fernando")
    real = time.time
    monkeypatch.setattr("app.incident.approval.time.time", lambda: real() + 5)
    with pytest.raises(ApprovalError, match="expired"):
        gate.consume(req.approval_id, tok, BIND)


def test_pin_enforced():
    gate = ApprovalGate("s", pin="4321")
    req = gate.request("inc", BIND)
    with pytest.raises(ApprovalError, match="PIN"):
        gate.approve(req.approval_id, "Fernando", pin="0000")
    assert gate.approve(req.approval_id, "Fernando", pin="4321")


def test_double_approve_refused():
    gate = ApprovalGate("s")
    req = gate.request("inc", BIND)
    gate.approve(req.approval_id, "A")
    with pytest.raises(ApprovalError, match="already approved"):
        gate.approve(req.approval_id, "B")
