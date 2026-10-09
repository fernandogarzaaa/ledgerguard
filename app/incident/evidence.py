"""Collect the Airwallex records behind an incident and lay them out as a timeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.airwallex.client import AirwallexClient, AirwallexError, lookback
from app.airwallex.models import Balance, Beneficiary, Conversion, Transfer, fmt, iso
from app.incident.invoices import InvoiceBook
from app.incident.models import Invoice, TimelineEvent

# A conversion bought in the payout currency within this window before the
# payout is treated as (inferred) funding for it.
CONVERSION_LINK_WINDOW = timedelta(hours=48)
FUNDING_CURRENCIES = ("USD", "EUR", "GBP")


@dataclass
class EvidenceBundle:
    invoice_id: str
    invoice: Invoice | None
    transfers: list[Transfer]
    inferred_transfer_ids: set[str]
    beneficiary: Beneficiary | None
    conversions: list[Conversion]
    balances: list[Balance]
    now: datetime
    errors: list[str] = field(default_factory=list)
    # (buy_currency, sell_currency) -> indicative units of sell per 1 buy
    rates: dict[tuple[str, str], Decimal] = field(default_factory=dict)

    def balance(self, currency: str) -> Decimal:
        for b in self.balances:
            if b.currency == currency:
                return b.available
        return Decimal(0)

    def transfers_sorted(self) -> list[Transfer]:
        epoch = datetime.min.replace(tzinfo=UTC)
        return sorted(self.transfers, key=lambda t: t.created_at or epoch)

    def snapshot(self) -> dict[str, Any]:
        """A stable digest of the evidence, used to bind approvals."""
        return {
            "invoice_id": self.invoice_id,
            "transfers": sorted((t.id, t.status, t.failure_type) for t in self.transfers),
            "beneficiary": self.beneficiary.id if self.beneficiary else None,
        }


def matches_invoice(t: Transfer, invoice_id: str) -> bool:
    if t.invoice_id and t.invoice_id.upper() == invoice_id.upper():
        return True
    return invoice_id.upper() in (t.reference or "").upper()


async def collect(
    client: AirwallexClient,
    book: InvoiceBook,
    invoice_id: str,
    *,
    lookback_days: int = 30,
    now: datetime | None = None,
) -> EvidenceBundle:
    now = now or datetime.now(UTC)
    invoice = book.get(invoice_id)
    errors: list[str] = []
    since = lookback(lookback_days, now)

    transfers: list[Transfer] = []
    inferred: set[str] = set()
    try:
        recent = await client.list_transfers(from_created_at=since)
        transfers = [t for t in recent if matches_invoice(t, invoice_id)]
        if invoice is not None:
            # A payout keyed in by hand (web app) may carry no invoice reference:
            # same beneficiary + same amount + currency is an inferred match.
            known = {t.id for t in transfers}
            for t in recent:
                if (
                    t.id not in known
                    and t.beneficiary_id == invoice.beneficiary_id
                    and t.transfer_amount == invoice.amount
                    and t.transfer_currency == invoice.currency
                ):
                    transfers.append(t)
                    inferred.add(t.id)
    except AirwallexError as exc:
        errors.append(f"transfers: {exc}")

    beneficiary: Beneficiary | None = None
    ben_id = invoice.beneficiary_id if invoice else ""
    if not ben_id and transfers:
        ben_id = transfers[0].beneficiary_id
    if ben_id:
        try:
            beneficiary = await client.get_beneficiary(ben_id)
        except AirwallexError as exc:
            errors.append(f"beneficiary: {exc}")

    conversions: list[Conversion] = []
    currencies = {t.transfer_currency for t in transfers} | (
        {invoice.currency} if invoice else set()
    )
    for cur in sorted(c for c in currencies if c):
        try:
            convs = await client.list_conversions(buy_currency=cur, from_created_at=since)
        except AirwallexError as exc:
            errors.append(f"conversions: {exc}")
            continue
        for c in convs:
            if any(_conversion_funds(c, t) for t in transfers):
                conversions.append(c)

    balances: list[Balance] = []
    try:
        balances = await client.get_balances()
    except AirwallexError as exc:
        errors.append(f"balances: {exc}")

    rates: dict[tuple[str, str], Decimal] = {}
    if invoice is not None:
        have = next((b.available for b in balances if b.currency == invoice.currency), Decimal(0))
        if have < invoice.amount:
            for src in FUNDING_CURRENCIES:
                if src == invoice.currency:
                    continue
                rate = await client.get_rate(invoice.currency, src)
                if rate is not None:
                    rates[(invoice.currency, src)] = rate

    return EvidenceBundle(
        rates=rates,
        invoice_id=invoice_id,
        invoice=invoice,
        transfers=transfers,
        inferred_transfer_ids=inferred,
        beneficiary=beneficiary,
        conversions=conversions,
        balances=balances,
        now=now,
        errors=errors,
    )


def _conversion_funds(c: Conversion, t: Transfer) -> bool:
    if c.buy_currency != t.transfer_currency or c.created_at is None or t.created_at is None:
        return False
    gap = t.created_at - c.created_at
    return timedelta(0) <= gap <= CONVERSION_LINK_WINDOW and c.buy_amount >= t.transfer_amount


def _sev(t: Transfer) -> str:
    if t.is_paid:
        return "ok"
    if t.is_failed:
        return "bad"
    return "warn"


def build_timeline(b: EvidenceBundle, reported_text: str = "") -> list[TimelineEvent]:
    ev: list[TimelineEvent] = []
    inv = b.invoice
    if inv and inv.due_date:
        ev.append(
            TimelineEvent(
                at=f"{inv.due_date}T00:00:00+0000",
                kind="invoice",
                title=f"{inv.invoice_id} due: {inv.amount} {inv.currency} to {inv.supplier}",
                ref=f"invoice:{inv.invoice_id}",
            )
        )
    if inv and inv.bank_details_changed_at:
        ev.append(
            TimelineEvent(
                at=iso(inv.bank_details_changed_at),
                kind="vendor_master",
                title="Supplier bank details changed in vendor master",
                detail=inv.bank_details_change_source,
                ref=f"vendor_master:{inv.invoice_id}",
                severity="warn",
            )
        )
    for c in b.conversions:
        if c.created_at:
            ev.append(
                TimelineEvent(
                    at=iso(c.created_at),
                    kind="conversion",
                    title=f"FX: sold {fmt(c.sell_amount)} {c.sell_currency}, bought "
                    f"{fmt(c.buy_amount)} {c.buy_currency} @ {c.client_rate}",
                    detail=f"{c.status} · {c.short_reference_id or c.id} (inferred funding link)",
                    ref=f"conversion:{c.id}",
                )
            )
    for t in b.transfers_sorted():
        label = t.short_reference_id or t.id
        inferred = (
            " (matched by beneficiary+amount, no invoice reference)"
            if (t.id in b.inferred_transfer_ids)
            else ""
        )
        if t.created_at:
            ev.append(
                TimelineEvent(
                    at=iso(t.created_at),
                    kind="transfer",
                    title=f"Payout {label} created: {fmt(t.transfer_amount)} {t.transfer_currency} "
                    f"via {t.transfer_method or '?'}{inferred}",
                    detail=f"request_id {t.request_id}",
                    ref=f"transfer:{t.id}",
                )
            )
        if t.updated_at:
            detail = ""
            if t.failure_type or t.failure_reason:
                detail = f"failure_type {t.failure_type or '?'} · {t.failure_reason}".strip()
                if t.status == "CANCELLED":
                    detail += " · CANCELLED here means the transfer failed, not a human cancel"
            ev.append(
                TimelineEvent(
                    at=iso(t.updated_at),
                    kind="transfer",
                    title=f"Payout {label} is {t.status}",
                    detail=detail,
                    ref=f"transfer:{t.id}",
                    severity=_sev(t),
                )
            )
    for bal in b.balances:
        if inv and bal.currency not in {inv.currency, "USD"}:
            continue
        ev.append(
            TimelineEvent(
                at=iso(b.now),
                kind="balance",
                title=f"Wallet {bal.currency}: {fmt(bal.available)} available",
                ref=f"balance:{bal.currency}",
            )
        )
    ev.append(
        TimelineEvent(
            at=iso(b.now),
            kind="incident",
            title="Incident reported",
            detail=reported_text[:280],
            ref="incident:report",
            severity="warn",
        )
    )
    ev.sort(key=lambda e: e.at)
    return ev
