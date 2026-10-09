"""The AP invoice book and incident-text parsing."""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.airwallex.models import dec, parse_ts
from app.incident.models import Invoice

INVOICE_RE = re.compile(r"\b(INV[-_ ]?\d{2,8})\b", re.IGNORECASE)
AMOUNT_RE = re.compile(
    r"\b(USD|EUR|GBP|AUD|SGD|HKD|CNY|JPY|CAD|NZD|CHF|PHP)\s?([0-9][0-9,]*(?:\.\d{1,2})?)\b"
    r"|([$€£])\s?([0-9][0-9,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}


def normalise_invoice_id(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    return f"INV-{digits}" if digits else raw.upper()


def parse_invoice_ids(text: str) -> list[str]:
    seen: list[str] = []
    for m in INVOICE_RE.finditer(text or ""):
        inv = normalise_invoice_id(m.group(1))
        if inv not in seen:
            seen.append(inv)
    return seen


def parse_claimed_amount(text: str) -> tuple[Decimal, str] | None:
    m = AMOUNT_RE.search(text or "")
    if not m:
        return None
    if m.group(1):
        cur, num = m.group(1).upper(), m.group(2)
    else:
        cur, num = SYMBOLS[m.group(3)], m.group(4)
    return Decimal(num.replace(",", "")), cur


def _invoice_from_dict(row: dict[str, Any]) -> Invoice:
    changed = row.get("bank_details_changed_at")
    return Invoice(
        invoice_id=normalise_invoice_id(str(row["invoice_id"])),
        supplier=str(row.get("supplier") or ""),
        beneficiary_id=str(row.get("beneficiary_id") or ""),
        amount=dec(row.get("amount")),
        currency=str(row.get("currency") or "").upper(),
        transfer_method=str(row.get("transfer_method") or "LOCAL").upper(),
        due_date=str(row.get("due_date") or ""),
        supplier_contact=str(row.get("supplier_contact") or ""),
        bank_details_changed_at=parse_ts(changed) if isinstance(changed, str) else None,
        bank_details_change_source=str(row.get("bank_details_change_source") or ""),
    )


class InvoiceBook:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._by_id: dict[str, Invoice] = {}
        for r in rows:
            inv = _invoice_from_dict(r)
            self._by_id[inv.invoice_id] = inv

    @classmethod
    def from_file(cls, path: str | Path) -> InvoiceBook:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = data.get("invoices", data) if isinstance(data, dict) else data
        return cls(list(rows))

    @classmethod
    def mock(cls, now: datetime) -> InvoiceBook:
        from app.airwallex import fixtures

        return cls(fixtures.invoice_book(now))

    def get(self, invoice_id: str) -> Invoice | None:
        return self._by_id.get(normalise_invoice_id(invoice_id))

    def find_by_supplier(self, text: str) -> list[Invoice]:
        low = (text or "").lower()
        return [
            inv
            for inv in self._by_id.values()
            if inv.supplier and inv.supplier.lower().split()[0] in low
        ]

    def all(self) -> list[Invoice]:
        return list(self._by_id.values())
