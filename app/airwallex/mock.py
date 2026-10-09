"""In-memory Airwallex sandbox simulator, exposed as an httpx transport.

It mirrors the sandbox semantics Ledgerguard depends on:
- login with x-client-id / x-api-key returns a bearer token;
- balances are a bare array (as observed live);
- transfers start SCHEDULED, debit the wallet (amount + fee), move through
  PROCESSING -> SENT -> PAID via the simulation endpoint, and a FAILED
  transition auto-cancels and refunds the amount (the fee is kept);
- request_id reuse is rejected with ``request_id_duplicate``;
- insufficient balance is rejected with ``balance_insufficient``;
- fault injection: ``fail_next_create_after_commit`` makes the next create
  commit the transfer but answer 500, to prove the client never double-pays.
"""

from __future__ import annotations

import copy
import itertools
import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx

from app.airwallex import fixtures
from app.airwallex.models import CANCELLABLE_STATUSES, dec, iso, parse_ts

MOCK_CLIENT_ID = "mock-client-id"
MOCK_API_KEY = "mock-api-key"

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "SCHEDULED": {"PROCESSING", "OVERDUE", "CANCELLED", "FAILED"},
    "OVERDUE": {"PROCESSING", "CANCELLED"},
    "PROCESSING": {"SENT", "FAILED"},
    "SENT": {"PAID", "FAILED"},
    "PAID": {"FAILED"},
    "FAILED": {"CANCELLED"},
}


def _j(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


def _err(status: int, code: str, message: str) -> httpx.Response:
    return _j(status, {"code": code, "message": message})


class MockAirwallex:
    def __init__(self, now: datetime | None = None) -> None:
        self._lock = threading.RLock()
        self.fail_next_create_after_commit = False
        self.reset(now)

    # -- state -----------------------------------------------------------------
    def reset(self, now: datetime | None = None) -> None:
        with self._lock:
            self.now = now or datetime.now(UTC)
            self.transfers: dict[str, dict[str, Any]] = {
                t["id"]: t for t in fixtures.transfers(self.now)
            }
            self.beneficiaries: dict[str, dict[str, Any]] = {
                b["id"]: copy.deepcopy(b) for b in fixtures.BENEFICIARIES
            }
            self.conversions: list[dict[str, Any]] = fixtures.conversions(self.now)
            self.balances: dict[str, Decimal] = {
                k: Decimal(v) for k, v in fixtures.balances().items()
            }
            self.used_request_ids: set[str] = {t["request_id"] for t in self.transfers.values()}
            self.tokens: set[str] = set()
            # Transfer ids hidden from broad list queries (simulates list lag);
            # a lookup by request_id still finds them.
            self.hide_from_list: set[str] = set()
            self._seq = itertools.count(1)
            self.create_calls = 0

    def clock(self) -> datetime:
        return datetime.now(UTC)

    def set_balance(self, currency: str, amount: str) -> None:
        with self._lock:
            self.balances[currency] = Decimal(amount)

    # -- transport -------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            try:
                return self._route(request)
            except Exception as exc:  # pragma: no cover - defensive
                return _err(500, "mock_error", str(exc))

    def _route(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method
        if path == "/api/v1/authentication/login" and method == "POST":
            return self._login(req)
        auth = req.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
            return _err(401, "credentials_invalid", "missing or invalid bearer token")
        q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
        body: dict[str, Any] = {}
        if req.content:
            try:
                body = json.loads(req.content)
            except ValueError:
                return _err(400, "invalid_argument", "body is not JSON")
        parts = [p for p in path.split("/") if p]
        # /api/v1/<...>
        rest = parts[2:]
        if rest == ["balances", "current"] and method == "GET":
            return self._balances()
        if rest == ["transfers"] and method == "GET":
            return self._list_transfers(q)
        if rest == ["transfers", "create"] and method == "POST":
            return self._create_transfer(body)
        if len(rest) == 3 and rest[:2] == ["transfers", "cancel"] and method == "POST":
            return self._cancel(rest[2])
        if len(rest) == 2 and rest[0] == "transfers" and method == "GET":
            t = self.transfers.get(rest[1])
            return _j(200, t) if t else _err(404, "not_found", "transfer not found")
        if (
            len(rest) == 4
            and rest[:2] == ["simulation", "transfers"]
            and rest[3] == "transition"
            and method == "POST"
        ):
            return self._transition(rest[2], body)
        if rest == ["beneficiaries"] and method == "GET":
            return _j(200, {"items": list(self.beneficiaries.values()), "has_more": False})
        if len(rest) == 2 and rest[0] == "beneficiaries" and method == "GET":
            b = self.beneficiaries.get(rest[1])
            return _j(200, b) if b else _err(404, "not_found", "beneficiary not found")
        if rest == ["fx", "conversions"] and method == "GET":
            return self._list_conversions(q)
        if rest == ["fx", "rates", "current"] and method == "GET":
            return self._rate(q)
        return _err(404, "not_found", f"mock has no route {method} {path}")

    # -- handlers ----------------------------------------------------------------
    def _login(self, req: httpx.Request) -> httpx.Response:
        if not req.headers.get("x-client-id") or not req.headers.get("x-api-key"):
            return _err(401, "credentials_invalid", "x-client-id and x-api-key are required")
        token = f"mock-token-{next(self._seq)}"
        self.tokens.add(token)
        exp = self.clock().timestamp() + 1800
        return _j(201, {"token": token, "expires_at": iso(datetime.fromtimestamp(exp, UTC))})

    def _balances(self) -> httpx.Response:
        rows = [
            {
                "account_type": "cash",
                "currency": cur,
                "available_amount": float(amt),
                "pending_amount": 0,
                "reserved_amount": 0,
                "prepayment_amount": 0,
                "total_amount": float(amt),
            }
            for cur, amt in sorted(self.balances.items())
        ]
        return _j(200, rows)

    def _list_transfers(self, q: dict[str, str]) -> httpx.Response:
        items = list(self.transfers.values())
        if q.get("request_id"):
            items = [t for t in items if t["request_id"] == q["request_id"]]
        else:
            items = [t for t in items if t["id"] not in self.hide_from_list]
        if q.get("status"):
            items = [t for t in items if t["status"] == q["status"]]
        if q.get("beneficiary_id"):
            items = [t for t in items if t["beneficiary_id"] == q["beneficiary_id"]]
        if q.get("from_created_at"):
            start = parse_ts(q["from_created_at"])
            if start is not None:
                items = [t for t in items if (parse_ts(t["created_at"]) or start) >= start]
        items.sort(key=lambda t: t["created_at"], reverse=True)
        return _j(200, {"items": items, "page_after": None, "page_before": None})

    def _create_transfer(self, body: dict[str, Any]) -> httpx.Response:
        self.create_calls += 1
        rid = str(body.get("request_id") or "")
        for f in ("request_id", "reason", "reference", "transfer_currency"):
            if not body.get(f):
                return _err(400, "field_required", f"{f} is required")
        if not 10 <= len(rid) <= 50:
            return _err(400, "018", "request_id must be 10-50 characters")
        if "payment_currency" in body:
            return _err(400, "001", "payment_currency is not a valid field")
        if rid in self.used_request_ids:
            return _err(400, "request_id_duplicate", "request_id used in the last 7 days")
        ben = self.beneficiaries.get(str(body.get("beneficiary_id") or ""))
        if ben is None:
            return _err(400, "validation_failed", "beneficiary_id not found")
        method = str(body.get("transfer_method") or ben["transfer_methods"][0])
        if method not in ("LOCAL", "SWIFT"):
            return _err(400, "validation_failed", "transfer_method must be LOCAL or SWIFT")
        cur = str(body["transfer_currency"])
        amount = dec(body.get("transfer_amount"))
        if amount <= 0:
            return _err(400, "validation_failed", "transfer_amount must be positive")
        src_cur = str(body.get("source_currency") or cur)
        fee = (
            Decimal(fixtures.SWIFT_FLAT_FEE.get(src_cur, "0")) if method == "SWIFT" else Decimal(0)
        )
        if src_cur != cur:
            rate = Decimal(fixtures.MOCK_RATES.get((cur, src_cur), "0"))
            if rate == 0:
                return _err(400, "currency_pair_invalid", f"{src_cur}->{cur} not supported")
            src_amount = (amount * rate).quantize(Decimal("0.01"))
        else:
            src_amount = amount
        debit = src_amount + fee
        if self.balances.get(src_cur, Decimal(0)) < debit:
            return _err(400, "balance_insufficient", f"{src_cur} balance too low for {debit}")
        self.balances[src_cur] = self.balances.get(src_cur, Decimal(0)) - debit
        self.used_request_ids.add(rid)
        n = next(self._seq)
        now = iso(self.clock())
        t = {
            "id": f"trf_mock_{n:04d}",
            "short_reference_id": f"P{now[2:10].replace('-', '')}-LG{n:04d}",
            "request_id": rid,
            "status": "SCHEDULED",
            "beneficiary_id": ben["id"],
            "transfer_amount": float(amount),
            "transfer_currency": cur,
            "source_amount": float(src_amount),
            "source_currency": src_cur,
            "transfer_method": method,
            "reference": body["reference"],
            "reason": body["reason"],
            "metadata": dict(body.get("metadata") or {}),
            "fee_amount": float(fee),
            "fee_currency": src_cur,
            "fee_paid_by": "PAYER",
            "created_at": now,
            "updated_at": now,
            "transfer_date": now[:10],
        }
        if src_cur != cur:
            t["conversion"] = {
                "currency_pair": f"{cur}{src_cur}",
                "rate": float(fixtures.MOCK_RATES[(cur, src_cur)]),
            }
        self.transfers[t["id"]] = t
        if self.fail_next_create_after_commit:
            self.fail_next_create_after_commit = False
            return _err(500, "service_unavailable", "simulated outage after commit")
        return _j(201, t)

    def _refund(self, t: dict[str, Any]) -> None:
        cur = str(t["source_currency"])
        self.balances[cur] = self.balances.get(cur, Decimal(0)) + dec(t["source_amount"])

    def _cancel(self, tid: str) -> httpx.Response:
        t = self.transfers.get(tid)
        if t is None:
            return _err(404, "not_found", "transfer not found")
        if t["status"] not in CANCELLABLE_STATUSES:
            return _err(400, "invalid_status", f"cannot cancel a {t['status']} transfer")
        t["status"] = "CANCELLED"
        t["failure_type"] = "CLIENT_REQUESTED"
        t["failure_reason"] = "Cancelled by client"
        t["updated_at"] = iso(self.clock())
        self._refund(t)
        return _j(200, {})

    def _transition(self, tid: str, body: dict[str, Any]) -> httpx.Response:
        t = self.transfers.get(tid)
        if t is None:
            return _err(404, "not_found", "transfer not found")
        nxt = str(body.get("next_status") or "").upper()
        if nxt not in ALLOWED_TRANSITIONS.get(t["status"], set()):
            return _err(400, "invalid_argument", f"cannot transition {t['status']} -> {nxt}")
        t["updated_at"] = iso(self.clock())
        if nxt == "FAILED":
            ftype = str(body.get("failure_type") or "")
            if ftype and t["status"] not in ("SENT", "PAID"):
                # Builder guide: a chosen failure_type applies only after SENT.
                ftype = ""
            t["failure_type"] = ftype or "OTHER"
            t["failure_reason"] = ftype or "simulation"
            # FAILED auto-cancels "in seconds"; the mock collapses that wait.
            t["status"] = "CANCELLED"
            self._refund(t)
        elif nxt == "CANCELLED":
            t["status"] = "CANCELLED"
            t["failure_type"] = t.get("failure_type") or ""
            t["failure_reason"] = "simulation"
            self._refund(t)
        else:
            t["status"] = nxt
        return _j(200, t)

    def _list_conversions(self, q: dict[str, str]) -> httpx.Response:
        items = list(self.conversions)
        if q.get("buy_currency"):
            items = [c for c in items if c["buy_currency"] == q["buy_currency"]]
        if q.get("from_created_at"):
            start = parse_ts(q["from_created_at"])
            if start is not None:
                items = [c for c in items if (parse_ts(c["created_at"]) or start) >= start]
        items.sort(key=lambda c: c["created_at"], reverse=True)
        return _j(200, {"items": items, "has_more": False})

    def _rate(self, q: dict[str, str]) -> httpx.Response:
        buy, sell = q.get("buy_currency", ""), q.get("sell_currency", "")
        rate = fixtures.MOCK_RATES.get((buy, sell))
        if rate is None:
            return _err(400, "currency_pair_invalid", f"{sell}->{buy} not supported")
        return _j(
            200,
            {"buy_currency": buy, "sell_currency": sell, "rate": float(rate), "rate_details": []},
        )
