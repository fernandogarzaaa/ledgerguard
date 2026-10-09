"""Deterministic incident classification. The model never decides this."""

from __future__ import annotations

import re
from datetime import timedelta

from app.airwallex.models import fmt
from app.incident.evidence import EvidenceBundle
from app.incident.models import CaseClass, Classification, Evidence

# failure_type -> family, from Airwallex "Failure reasons" recommended actions.
BAD_BENEFICIARY_FAILURES = frozenset(
    {
        "INVALID_ACCOUNT_NAME_OR_NUMBER",
        "INVALID_BANK_INFORMATION",
        "INVALID_BANK_OR_BRANCH_CODE",
        "INVALID_SWIFT_BIC_CODE",
        "INVALID_CORRESPONDENT_BANK_INFORMATION",
        "BENEFICIARY_NAME_MISMATCH",
        "ACCOUNT_CURRENCY_MISMATCH",
        "INVALID_BENEFICIARY_DETAILS",
        "INVALID_SPECIAL_CHARACTER",
        "ACCOUNT_CLOSED",
        "ACCOUNT_INACTIVE_OR_DORMANT",
        "ACCOUNT_UNDER_RESTRICTION",
    }
)
RETURNED_FAILURES = frozenset(
    {"BENEFICIARY_BANK_RETURNED", "BENEFICIARY_REQUESTED", "DUPLICATION_RETURN"}
)
# Safe to replace with a fresh payout once the cause is fixed (funds / timeout).
RETRYABLE_FAILURES = frozenset(
    {"INSUFFICIENT_FUNDS", "TRANSACTION_AMOUNT_NOT_COVERING_FEE", "CHANNEL_TIMEOUT"}
)
# Anything else (TM_SUSPENDED, CHANNEL_POLICY, RECALL_REQUESTED, SYSTEM_ERROR,
# INVALID_PAYMENT_PURPOSE, OTHER, UNKNOWN, ...) is escalated, never auto-replaced.

VENDOR_CHANGE_WINDOW = timedelta(hours=72)

_SUFFIXES = {"inc", "ltd", "llc", "co", "gmbh", "bv", "pte", "limited", "corp", "plc", "sa", "ag"}


def _norm_name(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return " ".join(w for w in words if w not in _SUFFIXES)


def failure_family(failure_type: str) -> str:
    ft = (failure_type or "").upper()
    if ft in BAD_BENEFICIARY_FAILURES:
        return "bad_beneficiary"
    if ft in RETURNED_FAILURES:
        return "returned"
    if ft in RETRYABLE_FAILURES:
        return "retryable"
    return "non_retryable"


def classify(b: EvidenceBundle) -> Classification:
    reasons: list[str] = []
    evidence: list[Evidence] = []
    flags: list[str] = []

    if b.invoice is None:
        return Classification(
            case=CaseClass.UNKNOWN_INVOICE,
            reasons=[f"{b.invoice_id} is not in the AP invoice book"],
            evidence=[Evidence(f"invoice:{b.invoice_id}", "no matching payable invoice")],
            flags=["unknown_invoice"],
        )
    inv = b.invoice
    evidence.append(
        Evidence(
            f"invoice:{inv.invoice_id}",
            f"{inv.amount} {inv.currency} owed to {inv.supplier}, due {inv.due_date or '?'}",
        )
    )

    # Vendor-master change shortly before (or after) the last payout attempt.
    if inv.bank_details_changed_at is not None:
        anchor = max((t.created_at for t in b.transfers if t.created_at is not None), default=b.now)
        if abs(anchor - inv.bank_details_changed_at) <= VENDOR_CHANGE_WINDOW or (
            b.now - inv.bank_details_changed_at <= timedelta(days=7)
        ):
            flags.append("recent_bank_detail_change")
            evidence.append(
                Evidence(
                    f"vendor_master:{inv.invoice_id}",
                    "bank details changed "
                    f"{inv.bank_details_changed_at:%Y-%m-%d %H:%M} UTC via "
                    f"{inv.bank_details_change_source or 'unknown channel'}",
                )
            )
    if b.beneficiary is not None:
        evidence.append(
            Evidence(
                f"beneficiary:{b.beneficiary.id}",
                f"{b.beneficiary.name} · account name '{b.beneficiary.account_name}' · "
                f"{b.beneficiary.account_number_masked} · {b.beneficiary.routing}",
            )
        )
        acct = _norm_name(b.beneficiary.account_name)
        if acct and acct not in {_norm_name(b.beneficiary.name), _norm_name(inv.supplier)}:
            flags.append("account_name_mismatch")
    if b.inferred_transfer_ids:
        flags.append("transfer_without_invoice_reference")

    live = [t for t in b.transfers if t.is_live]
    paid = [t for t in live if t.is_paid]
    in_flight = [t for t in live if t.is_in_flight]
    failed = [t for t in b.transfers if t.is_failed]

    for t in b.transfers_sorted():
        line = f"{t.status} {fmt(t.transfer_amount)} {t.transfer_currency}"
        if t.failure_type:
            line += f" failure_type={t.failure_type}"
        evidence.append(Evidence(f"transfer:{t.id}", line))
        if t.transfer_amount != inv.amount or t.transfer_currency != inv.currency:
            flags.append(f"amount_mismatch:{t.id}")

    if len(live) >= 2:
        reasons.append(
            f"{len(live)} live payouts exist for {inv.invoice_id} "
            f"({', '.join(f'{t.id}={t.status}' for t in live)}); paying again would be a third"
        )
        return Classification(CaseClass.DUPLICATE_RISK, reasons, evidence, flags)

    if len(live) == 1:
        t = live[0]
        if paid:
            reasons.append(
                f"payout {t.short_reference_id or t.id} is PAID "
                f"(updated {t.updated_at:%Y-%m-%d %H:%M} UTC)"
                if t.updated_at
                else f"payout {t.id} is PAID"
            )
            reasons.append("PAID can still turn FAILED on a late bank return; none seen so far")
            return Classification(CaseClass.PAID_CONFIRMED, reasons, evidence, flags)
        reasons.append(
            f"payout {t.short_reference_id or t.id} is {t.status}: still in flight, not final"
        )
        if in_flight and t.status == "OVERDUE":
            flags.append("funding_overdue")
        return Classification(CaseClass.IN_FLIGHT, reasons, evidence, flags)

    if failed:
        last = max(failed, key=lambda t: t.updated_at or t.created_at or b.now)
        fam = failure_family(last.failure_type)
        reasons.append(
            f"latest payout {last.short_reference_id or last.id} ended {last.status} with "
            f"failure_type {last.failure_type or 'none given'} ({last.failure_reason or '-'})"
        )
        if last.status == "CANCELLED":
            reasons.append("CANCELLED is how Airwallex ends a failed transfer, not a human cancel")
        if fam == "bad_beneficiary":
            return Classification(CaseClass.BAD_BENEFICIARY, reasons, evidence, flags)
        if fam == "returned":
            return Classification(CaseClass.RETURNED, reasons, evidence, flags)
        flags.append(f"failure_family:{fam}")
        return Classification(CaseClass.FAILED, reasons, evidence, flags)

    reasons.append(
        f"no payout referencing {inv.invoice_id} in the last lookback window; the invoice is unpaid"
    )
    return Classification(CaseClass.TRULY_UNPAID, reasons, evidence, flags)
