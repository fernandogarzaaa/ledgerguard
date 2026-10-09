"""Normalised views of Airwallex records.

Airwallex returns large objects whose exact shape varies by API version. These
parsers keep the raw payload and pull out only what the incident logic needs,
tolerating both documented failure shapes (see docs/AIRWALLEX_VERIFY.md).
Amounts are major units, held as Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# Transfer status vocabulary (API version >= 2025-06-30).
IN_FLIGHT_STATUSES = frozenset({"IN_APPROVAL", "SCHEDULED", "OVERDUE", "PROCESSING", "SENT"})
PAID_STATUSES = frozenset({"PAID"})
FAILED_STATUSES = frozenset({"FAILED", "CANCELLED"})
APPROVAL_DEAD_STATUSES = frozenset({"APPROVAL_RECALLED", "APPROVAL_REJECTED", "APPROVAL_BLOCKED"})
# Per "Handle failed transfers": SCHEDULED / PROCESSING / OVERDUE can be cancelled.
# SENT and PAID cannot (the money has left Airwallex). Verify live: AIRWALLEX_VERIFY.md
CANCELLABLE_STATUSES = frozenset({"SCHEDULED", "OVERDUE", "PROCESSING"})


def dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def fmt(value: Decimal | None) -> str:
    """Money for display: two decimals, no float artefacts (4000.0 -> 4000.00)."""
    if value is None:
        return ""
    return str(value.quantize(Decimal("0.01")))


def parse_ts(value: Any) -> datetime | None:
    """Parse Airwallex timestamps such as 2017-03-22T16:08:02+1100 or ISO-8601."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    # +1100 -> +11:00 so fromisoformat accepts it on every supported Python.
    if len(text) >= 5 and text[-5] in "+-" and text[-4:].isdigit():
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        try:
            ts = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


@dataclass
class Transfer:
    id: str
    request_id: str
    status: str
    transfer_amount: Decimal
    transfer_currency: str
    source_amount: Decimal
    source_currency: str
    beneficiary_id: str
    reference: str
    transfer_method: str
    created_at: datetime | None
    updated_at: datetime | None
    short_reference_id: str = ""
    fee_amount: Decimal | None = None
    fee_currency: str = ""
    failure_type: str = ""
    failure_reason: str = ""
    failure_code: str = ""
    failure_shape: str = ""  # "top_level" | "nested" | ""
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def invoice_id(self) -> str:
        inv = self.metadata.get("invoice_id") if self.metadata else None
        return str(inv) if inv else ""

    @property
    def is_in_flight(self) -> bool:
        return self.status in IN_FLIGHT_STATUSES

    @property
    def is_paid(self) -> bool:
        return self.status in PAID_STATUSES

    @property
    def is_failed(self) -> bool:
        return self.status in FAILED_STATUSES or self.status in APPROVAL_DEAD_STATUSES

    @property
    def is_live(self) -> bool:
        """Money is out, or may still go out. A live transfer blocks a new payout."""
        return self.is_in_flight or self.is_paid

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Transfer:
        failure_type = str(data.get("failure_type") or "")
        failure_reason = str(data.get("failure_reason") or "")
        failure_code = ""
        shape = "top_level" if (failure_type or failure_reason) else ""
        nested = data.get("failure")
        if isinstance(nested, dict) and nested:
            details = nested.get("details") if isinstance(nested.get("details"), dict) else {}
            failure_type = failure_type or str((details or {}).get("type") or "")
            failure_reason = failure_reason or str(nested.get("message") or "")
            failure_code = str(nested.get("code") or "")
            shape = shape or "nested"
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        fee = data.get("fee_amount")
        return cls(
            id=str(data.get("id") or ""),
            request_id=str(data.get("request_id") or ""),
            status=str(data.get("status") or "UNKNOWN").upper(),
            transfer_amount=dec(data.get("transfer_amount")),
            transfer_currency=str(data.get("transfer_currency") or ""),
            source_amount=dec(data.get("source_amount") or data.get("transfer_amount")),
            source_currency=str(data.get("source_currency") or data.get("transfer_currency") or ""),
            beneficiary_id=str(data.get("beneficiary_id") or ""),
            reference=str(data.get("reference") or ""),
            transfer_method=str(data.get("transfer_method") or ""),
            created_at=parse_ts(data.get("created_at")),
            updated_at=parse_ts(data.get("updated_at")),
            short_reference_id=str(data.get("short_reference_id") or ""),
            fee_amount=dec(fee) if fee is not None else None,
            fee_currency=str(data.get("fee_currency") or ""),
            failure_type=failure_type.upper(),
            failure_reason=failure_reason,
            failure_code=failure_code,
            failure_shape=shape,
            metadata=dict(meta or {}),
            raw=data,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "short_reference_id": self.short_reference_id,
            "request_id": self.request_id,
            "status": self.status,
            "amount": f"{fmt(self.transfer_amount)} {self.transfer_currency}",
            "source": f"{fmt(self.source_amount)} {self.source_currency}",
            "method": self.transfer_method,
            "beneficiary_id": self.beneficiary_id,
            "reference": self.reference,
            "invoice_id": self.invoice_id,
            "created_at": iso(self.created_at) if self.created_at else None,
            "updated_at": iso(self.updated_at) if self.updated_at else None,
            "fee": f"{fmt(self.fee_amount)} {self.fee_currency}"
            if self.fee_amount is not None
            else None,
            "failure_type": self.failure_type or None,
            "failure_reason": self.failure_reason or None,
            "failure_code": self.failure_code or None,
        }


@dataclass
class Beneficiary:
    id: str
    name: str
    bank_country_code: str
    account_currency: str
    account_name: str
    account_number_masked: str
    routing: str
    transfer_methods: list[str]
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Beneficiary:
        ben = data.get("beneficiary") if isinstance(data.get("beneficiary"), dict) else data
        ben = ben or {}
        bank = ben.get("bank_details") if isinstance(ben.get("bank_details"), dict) else {}
        bank = bank or {}
        number = str(bank.get("account_number") or bank.get("iban") or "")
        masked = f"••••{number[-4:]}" if len(number) >= 4 else number
        name = (
            ben.get("company_name")
            or " ".join(x for x in (ben.get("first_name"), ben.get("last_name")) if x)
            or data.get("nickname")
            or bank.get("account_name")
            or ""
        )
        routing = ""
        if bank.get("account_routing_type1"):
            routing = f"{bank.get('account_routing_type1')}:{bank.get('account_routing_value1')}"
        elif bank.get("swift_code"):
            routing = f"swift:{bank.get('swift_code')}"
        return cls(
            id=str(data.get("id") or data.get("beneficiary_id") or ""),
            name=str(name),
            bank_country_code=str(bank.get("bank_country_code") or ""),
            account_currency=str(bank.get("account_currency") or ""),
            account_name=str(bank.get("account_name") or ""),
            account_number_masked=masked,
            routing=routing,
            transfer_methods=[str(m) for m in (data.get("transfer_methods") or [])],
            raw=data,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "bank_country": self.bank_country_code,
            "currency": self.account_currency,
            "account_name": self.account_name,
            "account": self.account_number_masked,
            "routing": self.routing,
            "transfer_methods": self.transfer_methods,
        }


@dataclass
class Conversion:
    id: str
    request_id: str
    status: str
    buy_currency: str
    buy_amount: Decimal
    sell_currency: str
    sell_amount: Decimal
    client_rate: Decimal
    created_at: datetime | None
    short_reference_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Conversion:
        return cls(
            id=str(data.get("conversion_id") or data.get("id") or ""),
            request_id=str(data.get("request_id") or ""),
            status=str(data.get("status") or "").upper(),
            buy_currency=str(data.get("buy_currency") or ""),
            buy_amount=dec(data.get("buy_amount")),
            sell_currency=str(data.get("sell_currency") or ""),
            sell_amount=dec(data.get("sell_amount")),
            client_rate=dec(data.get("client_rate")),
            created_at=parse_ts(data.get("created_at")),
            short_reference_id=str(data.get("short_reference_id") or ""),
            raw=data,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "short_reference_id": self.short_reference_id,
            "status": self.status,
            "sold": f"{fmt(self.sell_amount)} {self.sell_currency}",
            "bought": f"{fmt(self.buy_amount)} {self.buy_currency}",
            "client_rate": str(self.client_rate),
            "created_at": iso(self.created_at) if self.created_at else None,
        }


@dataclass
class Balance:
    currency: str
    available: Decimal
    pending: Decimal
    reserved: Decimal
    total: Decimal

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Balance:
        return cls(
            currency=str(data.get("currency") or ""),
            available=dec(data.get("available_amount")),
            pending=dec(data.get("pending_amount")),
            reserved=dec(data.get("reserved_amount")),
            total=dec(data.get("total_amount")),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "available": fmt(self.available),
            "pending": fmt(self.pending),
            "reserved": fmt(self.reserved),
            "total": fmt(self.total),
        }
