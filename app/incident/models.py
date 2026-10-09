"""Incident domain objects."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class CaseClass(StrEnum):
    IN_FLIGHT = "IN_FLIGHT"
    PAID_CONFIRMED = "PAID_CONFIRMED"
    FAILED = "FAILED"
    RETURNED = "RETURNED"
    BAD_BENEFICIARY = "BAD_BENEFICIARY"
    TRULY_UNPAID = "TRULY_UNPAID"
    DUPLICATE_RISK = "DUPLICATE_RISK"
    UNKNOWN_INVOICE = "UNKNOWN_INVOICE"


class ActionKind(StrEnum):
    WAIT_AND_MONITOR = "WAIT_AND_MONITOR"
    SEND_PROOF_OF_PAYMENT = "SEND_PROOF_OF_PAYMENT"
    REPLACE_PAYOUT = "REPLACE_PAYOUT"
    PAY_INVOICE = "PAY_INVOICE"
    CANCEL_DUPLICATE = "CANCEL_DUPLICATE"
    REQUEST_BANK_CONFIRMATION = "REQUEST_BANK_CONFIRMATION"
    VERIFY_BENEFICIARY = "VERIFY_BENEFICIARY"
    ESCALATE = "ESCALATE"


# Actions that move money (or stop money moving) and therefore go through the
# duplicate guard and the Airwallex write path.
MONEY_ACTIONS = frozenset(
    {ActionKind.REPLACE_PAYOUT, ActionKind.PAY_INVOICE, ActionKind.CANCEL_DUPLICATE}
)


@dataclass
class Invoice:
    invoice_id: str
    supplier: str
    beneficiary_id: str
    amount: Decimal
    currency: str
    transfer_method: str = "LOCAL"
    due_date: str = ""
    supplier_contact: str = ""
    bank_details_changed_at: datetime | None = None
    bank_details_change_source: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "invoice_id": self.invoice_id,
            "supplier": self.supplier,
            "beneficiary_id": self.beneficiary_id,
            "amount": f"{self.amount} {self.currency}",
            "transfer_method": self.transfer_method,
            "due_date": self.due_date,
            "supplier_contact": self.supplier_contact,
            "bank_details_changed_at": (
                self.bank_details_changed_at.isoformat() if self.bank_details_changed_at else None
            ),
            "bank_details_change_source": self.bank_details_change_source or None,
        }


@dataclass
class TimelineEvent:
    at: str  # ISO timestamp
    kind: str  # invoice | transfer | conversion | balance | vendor_master | incident
    title: str
    detail: str = ""
    ref: str = ""
    severity: str = "info"  # info | ok | warn | bad

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Evidence:
    ref: str  # e.g. transfer:trf_123, balance:USD
    claim: str  # what this piece of evidence shows

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Classification:
    case: CaseClass
    reasons: list[str]
    evidence: list[Evidence]
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case.value,
            "reasons": self.reasons,
            "evidence": [e.to_dict() for e in self.evidence],
            "flags": self.flags,
        }


@dataclass
class ProposedAction:
    kind: ActionKind
    title: str
    rationale: str
    cost_note: str
    risk_note: str
    risk_level: str  # low | medium | high
    evidence: list[Evidence]
    moves_money: bool = False
    # For money actions: exactly what will be sent to Airwallex.
    amount: Decimal | None = None
    currency: str = ""
    source_currency: str = ""
    beneficiary_id: str = ""
    transfer_method: str = ""
    request_id: str = ""
    target_transfer_id: str = ""  # for CANCEL_DUPLICATE / reference to the original
    invoice_id: str = ""
    next_check_at: str = ""
    evidence_digest: str = ""  # hash of transfer ids/statuses the decision was made on

    def binding(self) -> dict[str, Any]:
        """The fields an approval is bound to. Change any of them -> approval void."""
        return {
            "kind": self.kind.value,
            "invoice_id": self.invoice_id,
            "amount": str(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "source_currency": self.source_currency,
            "beneficiary_id": self.beneficiary_id,
            "transfer_method": self.transfer_method,
            "request_id": self.request_id,
            "target_transfer_id": self.target_transfer_id,
            "evidence": sorted(e.ref for e in self.evidence),
            "evidence_digest": self.evidence_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.binding()
        d.update(
            {
                "title": self.title,
                "rationale": self.rationale,
                "cost_note": self.cost_note,
                "risk_note": self.risk_note,
                "risk_level": self.risk_level,
                "moves_money": self.moves_money,
                "evidence": [e.to_dict() for e in self.evidence],
                "next_check_at": self.next_check_at or None,
            }
        )
        return d
