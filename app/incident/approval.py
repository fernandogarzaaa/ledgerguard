"""Human approval gate.

An approval is bound to a fingerprint of the exact action (kind, amount,
currency, beneficiary, request_id, target transfer, evidence refs). The token is
an HMAC only the server can mint, it is single-use, and it expires. Executing a
different action than the one approved, reusing a token, or presenting a forged
or expired token is refused. Approval is never exposed as an agent/MCP tool.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any


class ApprovalError(RuntimeError):
    pass


def fingerprint(binding: dict[str, Any]) -> str:
    blob = json.dumps(binding, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class ApprovalRequest:
    approval_id: str
    incident_id: str
    fingerprint: str
    binding: dict[str, Any]
    created_at: float
    expires_at: float
    approved_by: str = ""
    approved_at: float = 0.0
    token_hash: str = ""
    used: bool = False

    @property
    def status(self) -> str:
        if self.used:
            return "used"
        if time.time() > self.expires_at:
            return "expired"
        return "approved" if self.approved_by else "pending"

    def public(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "incident_id": self.incident_id,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "approved_by": self.approved_by or None,
            "expires_in_s": max(0, int(self.expires_at - time.time())),
        }


class ApprovalGate:
    def __init__(self, secret: str, ttl_s: int = 900, pin: str = "") -> None:
        if not secret:
            raise ValueError("approval secret required")
        self._secret = secret.encode()
        self._ttl = ttl_s
        self._pin = pin
        self._mutex = threading.Lock()
        self._requests: dict[str, ApprovalRequest] = {}

    @property
    def requires_pin(self) -> bool:
        return bool(self._pin)

    def request(self, incident_id: str, binding: dict[str, Any]) -> ApprovalRequest:
        now = time.time()
        req = ApprovalRequest(
            approval_id=f"apr_{secrets.token_hex(8)}",
            incident_id=incident_id,
            fingerprint=fingerprint(binding),
            binding=binding,
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._mutex:
            self._requests[req.approval_id] = req
        return req

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._requests.get(approval_id)

    def _mint(self, req: ApprovalRequest, approver: str) -> str:
        msg = f"{req.approval_id}|{req.fingerprint}|{approver}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def approve(self, approval_id: str, approver: str, pin: str = "") -> str:
        approver = (approver or "").strip()
        if not approver:
            raise ApprovalError("an approver name is required")
        if approver.lower() in {"agent", "ledgerguard", "llm", "model", "system"}:
            raise ApprovalError("the agent cannot approve its own actions")
        if self._pin and not hmac.compare_digest(pin or "", self._pin):
            raise ApprovalError("approval PIN is wrong")
        with self._mutex:
            req = self._requests.get(approval_id)
            if req is None:
                raise ApprovalError("unknown approval request")
            if req.status in ("used", "expired"):
                raise ApprovalError(f"approval request is {req.status}")
            if req.approved_by:
                raise ApprovalError("already approved")
            token = self._mint(req, approver)
            req.approved_by = approver
            req.approved_at = time.time()
            req.token_hash = hashlib.sha256(token.encode()).hexdigest()
            return token

    def consume(self, approval_id: str, token: str, binding: dict[str, Any]) -> ApprovalRequest:
        """Verify and burn the token for exactly this action. Raises ApprovalError."""
        with self._mutex:
            req = self._requests.get(approval_id)
            if req is None:
                raise ApprovalError("unknown approval request")
            if req.used:
                raise ApprovalError("approval token already used")
            if time.time() > req.expires_at:
                raise ApprovalError("approval expired; re-investigate and approve again")
            if not req.approved_by:
                raise ApprovalError("not approved by a person yet")
            expected = self._mint(req, req.approved_by)
            if not hmac.compare_digest(expected, token or ""):
                raise ApprovalError("approval token is invalid")
            if fingerprint(binding) != req.fingerprint:
                raise ApprovalError(
                    "the action changed since it was approved (amount, beneficiary, evidence "
                    "or request_id differ); re-investigate and approve again"
                )
            req.used = True
            return req
