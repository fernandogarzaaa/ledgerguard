"""Turn a classification into exactly ONE proposed action, with evidence, cost and risk.

All thresholds and money maths live here, in code. The request_id for a money
action is derived deterministically from the incident's facts, so the same
incident always proposes the same request_id: re-investigating and approving
twice can only ever hit Airwallex's idempotency check, never a second payout.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.airwallex import fixtures
from app.airwallex.models import CANCELLABLE_STATUSES, Transfer, fmt, iso
from app.incident.classifier import failure_family
from app.incident.evidence import EvidenceBundle
from app.incident.models import ActionKind, CaseClass, Classification, Evidence, ProposedAction

# How long a payout may sit SENT/PROCESSING before we stop waiting and chase it.
IN_FLIGHT_PATIENCE = {"LOCAL": timedelta(hours=72), "SWIFT": timedelta(hours=120)}
RECHECK_IN = timedelta(hours=12)
# Preferred funding currencies when the invoice currency is short.
FUNDING_PREFERENCE = ("USD", "EUR", "GBP")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class PolicyConfig:
    swift_fees: dict[str, str] | None = None

    def swift_fee(self, currency: str) -> Decimal | None:
        fees = self.swift_fees if self.swift_fees is not None else fixtures.SWIFT_FLAT_FEE
        raw = fees.get(currency)
        return Decimal(raw) if raw is not None else None


def make_request_id(*parts: str) -> str:
    """Deterministic, 10-50 char request_id (Airwallex code 018 otherwise)."""
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]
    head = (parts[0] if parts else "x").lower().replace("-", "")[:12]
    rid = f"lg-{head}-{digest}"
    assert 10 <= len(rid) <= 50
    return rid


def _money(x: Decimal) -> str:
    return f"{x.quantize(CENT, rounding=ROUND_HALF_UP):,}"


def _fee_for(method: str, currency: str, cfg: PolicyConfig) -> tuple[Decimal, str]:
    if method != "SWIFT":
        return Decimal(0), "LOCAL payouts carry no Airwallex fee"
    fee = cfg.swift_fee(currency)
    if fee is None:
        return Decimal(0), (
            f"SWIFT fee for {currency} unknown here; Airwallex returns fee_amount on the transfer"
        )
    return fee, f"SWIFT flat fee {currency} {_money(fee)} (builder guide; verify live)"


def _funding(
    b: EvidenceBundle, amount: Decimal, currency: str, method: str, cfg: PolicyConfig
) -> tuple[str, Decimal, str, bool]:
    """Pick the source currency. Returns (source_currency, est_debit, note, affordable)."""
    fee, fee_note = _fee_for(method, currency, cfg)
    need = amount + fee
    have = b.balance(currency)
    if have >= need:
        return (
            currency,
            need,
            f"Debits {currency} {_money(need)} from the {currency} wallet "
            f"({_money(have)} available → {_money(have - need)} after). {fee_note}.",
            True,
        )
    for src in FUNDING_PREFERENCE:
        if src == currency:
            continue
        rate = b.rates.get((currency, src))
        if rate is None:
            continue
        est = (amount * Decimal(rate)).quantize(CENT) + (fee * Decimal(rate)).quantize(CENT)
        if b.balance(src) >= est:
            return (
                src,
                est,
                f"{currency} wallet has only {_money(have)}; fund from {src}: about "
                f"{src} {_money(est)} at indicative {rate} {src}/{currency} (rate locks when "
                f"the transfer is created). {fee_note}.",
                True,
            )
    return (
        currency,
        need,
        f"Needs {currency} {_money(need)}; the {currency} wallet has {_money(have)} and no "
        f"other wallet covers it. {fee_note}.",
        False,
    )


EPOCH = datetime.min.replace(tzinfo=UTC)


def _latest(ts: list[Transfer]) -> Transfer:
    return max(ts, key=lambda t: (t.updated_at or t.created_at or EPOCH, t.id))


def propose(
    b: EvidenceBundle, c: Classification, cfg: PolicyConfig | None = None
) -> ProposedAction:
    cfg = cfg or PolicyConfig()
    inv = b.invoice
    ev = list(c.evidence)

    if c.case is CaseClass.UNKNOWN_INVOICE or inv is None:
        return ProposedAction(
            kind=ActionKind.ESCALATE,
            title=f"Escalate: {b.invoice_id} is not a known payable",
            rationale="No AP record means no amount or beneficiary we can trust. A person must "
            "confirm the invoice exists before anything is paid.",
            cost_note="No money moves.",
            risk_note="Paying an invoice we cannot find in AP is a classic invoice-fraud pattern.",
            risk_level="high",
            evidence=ev,
            invoice_id=b.invoice_id,
        )

    live = [t for t in b.transfers if t.is_live]
    failed = [t for t in b.transfers if t.is_failed]

    if c.case is CaseClass.PAID_CONFIRMED:
        t = live[0]
        return ProposedAction(
            kind=ActionKind.SEND_PROOF_OF_PAYMENT,
            title=f"Send proof of payment for {inv.invoice_id}",
            rationale=f"Airwallex shows {t.short_reference_id or t.id} PAID for "
            f"{fmt(t.transfer_amount)} {t.transfer_currency}. Paying again would pay twice.",
            cost_note="No money moves.",
            risk_note="Low. Keep watching for a late payout.transfer.failed on the original.",
            risk_level="low",
            evidence=ev,
            invoice_id=inv.invoice_id,
            target_transfer_id=t.id,
        )

    if c.case is CaseClass.IN_FLIGHT:
        t = live[0]
        patience = IN_FLIGHT_PATIENCE.get(t.transfer_method or "LOCAL", IN_FLIGHT_PATIENCE["LOCAL"])
        age = b.now - (t.created_at or b.now)
        if t.status == "OVERDUE" or age > patience:
            why = (
                "funding is OVERDUE"
                if t.status == "OVERDUE"
                else f"it has been in flight {age.total_seconds() / 3600:.0f}h, past the "
                f"{patience.total_seconds() / 3600:.0f}h {t.transfer_method or 'LOCAL'} window"
            )
            return ProposedAction(
                kind=ActionKind.ESCALATE,
                title=f"Escalate the stuck payout {t.short_reference_id or t.id} to Airwallex",
                rationale=f"The original payout is {t.status} and {why}. Do not send a second "
                "payout while the first can still land; ask Airwallex to trace it.",
                cost_note="No money moves.",
                risk_note="A replacement now would very likely pay twice.",
                risk_level="medium",
                evidence=ev,
                invoice_id=inv.invoice_id,
                target_transfer_id=t.id,
            )
        nxt = iso(b.now + RECHECK_IN)
        return ProposedAction(
            kind=ActionKind.WAIT_AND_MONITOR,
            title=f"Wait: {t.short_reference_id or t.id} is {t.status}",
            rationale=f"The payout is {t.status}, which is in flight and not final. It has been "
            f"{age.total_seconds() / 3600:.0f}h of a {patience.total_seconds() / 3600:.0f}h "
            f"{t.transfer_method or 'LOCAL'} window. Share the reference with the supplier and "
            "recheck; a webhook will update the incident if it lands or fails first.",
            cost_note="No money moves.",
            risk_note="Sending a replacement while this is SENT is the most common way teams "
            "pay twice.",
            risk_level="low",
            evidence=ev,
            invoice_id=inv.invoice_id,
            target_transfer_id=t.id,
            next_check_at=nxt,
        )

    if c.case is CaseClass.DUPLICATE_RISK:
        cancellable = [t for t in live if t.status in CANCELLABLE_STATUSES]
        settled = [t for t in live if t.status not in CANCELLABLE_STATUSES]
        if cancellable and settled:
            dup = max(cancellable, key=lambda t: (t.created_at or b.now, t.id))
            keep = _latest(settled)
            return ProposedAction(
                kind=ActionKind.CANCEL_DUPLICATE,
                title=f"Cancel duplicate payout {dup.short_reference_id or dup.id}",
                rationale=f"{keep.short_reference_id or keep.id} is already {keep.status} for "
                f"{inv.invoice_id}. {dup.short_reference_id or dup.id} is a second payout still "
                f"{dup.status} and can be cancelled before it leaves Airwallex.",
                cost_note=f"Stops {fmt(dup.transfer_amount)} {dup.transfer_currency} going out "
                "twice; the amount returns to the wallet (an FX delta fee may apply if it "
                "converted).",
                risk_note="Low if done now; once it is SENT it can no longer be cancelled.",
                risk_level="medium",
                evidence=ev,
                moves_money=True,
                amount=dup.transfer_amount,
                currency=dup.transfer_currency,
                beneficiary_id=dup.beneficiary_id,
                invoice_id=inv.invoice_id,
                target_transfer_id=dup.id,
                request_id=make_request_id(inv.invoice_id, "cancel", dup.id),
            )
        return ProposedAction(
            kind=ActionKind.ESCALATE,
            title=f"Escalate: {inv.invoice_id} has already gone out more than once",
            rationale="More than one payout has left Airwallex for this invoice; none can be "
            "cancelled. Ask the supplier to return the duplicate.",
            cost_note=f"Potential overpayment of {inv.amount} {inv.currency} to recover.",
            risk_note="High: recovery depends on the supplier.",
            risk_level="high",
            evidence=ev,
            invoice_id=inv.invoice_id,
        )

    if c.case is CaseClass.BAD_BENEFICIARY or (
        c.case is CaseClass.TRULY_UNPAID and "recent_bank_detail_change" in c.flags
    ):
        last = _latest(failed) if failed else None
        bits = []
        if last is not None:
            bits.append(f"the last payout failed with {last.failure_type}")
        if "recent_bank_detail_change" in c.flags:
            bits.append("the supplier's bank details were changed shortly before it")
        if "account_name_mismatch" in c.flags:
            bits.append("the account name on file does not match the supplier")
        return ProposedAction(
            kind=ActionKind.VERIFY_BENEFICIARY,
            title=f"Hold and verify {inv.supplier}'s bank details",
            rationale="Do not pay: "
            + "; ".join(bits or ["beneficiary details are suspect"])
            + ". Confirm the account by calling a contact already on file (not one from the "
            "change request), then update the beneficiary and re-run this incident.",
            cost_note="No money moves.",
            risk_note="High: a bank-detail change just before payment is the signature of "
            "business-email-compromise fraud.",
            risk_level="high",
            evidence=ev,
            invoice_id=inv.invoice_id,
            target_transfer_id=last.id if last else "",
        )

    if c.case is CaseClass.RETURNED:
        last = _latest(failed)
        return ProposedAction(
            kind=ActionKind.REQUEST_BANK_CONFIRMATION,
            title=f"Ask {inv.supplier} to confirm the account with their bank",
            rationale=f"{last.short_reference_id or last.id} was returned by the beneficiary bank "
            f"({last.failure_type}); the funds are back in the wallet. Re-sending to the same "
            "account before the supplier confirms it will most likely bounce again.",
            cost_note=f"No money moves now. {fmt(last.transfer_amount)} "
            f"{last.transfer_currency} is back in the wallet "
            f"({_money(b.balance(last.source_currency))} {last.source_currency} available).",
            risk_note="Medium: a second return costs another round of bank fees and days.",
            risk_level="medium",
            evidence=ev,
            invoice_id=inv.invoice_id,
            target_transfer_id=last.id,
        )

    if c.case is CaseClass.FAILED:
        last = _latest(failed)
        if failure_family(last.failure_type) != "retryable":
            return ProposedAction(
                kind=ActionKind.ESCALATE,
                title=f"Escalate {last.short_reference_id or last.id} to Airwallex support",
                rationale=f"The payout failed with {last.failure_type or 'no failure_type'} "
                f"({last.failure_reason or '-'}). That is a compliance/policy or unexplained "
                "failure: Airwallex recommends contacting them, not resending.",
                cost_note="No money moves.",
                risk_note="High: resending after a compliance hold can freeze the account.",
                risk_level="high",
                evidence=ev,
                invoice_id=inv.invoice_id,
                target_transfer_id=last.id,
            )
        return _payout_action(
            b,
            ActionKind.REPLACE_PAYOUT,
            ev,
            cfg,
            title=f"Replace failed payout {last.short_reference_id or last.id}",
            why=f"The original ended CANCELLED with {last.failure_type} (retryable) and its "
            "funds are back in the wallet. A new transfer with a NEW request_id goes to the same "
            "verified beneficiary; the original stays cancelled and is recorded in the report.",
            target=last.id,
            extra_fee_note=" The first transfer's fee is non-refundable, so this is a second fee."
            if last.transfer_method == "SWIFT"
            else "",
        )

    # TRULY_UNPAID
    return _payout_action(
        b,
        ActionKind.PAY_INVOICE,
        ev,
        cfg,
        title=f"Pay {inv.invoice_id} now",
        why=f"No payout for {inv.invoice_id} exists in Airwallex and nothing is in flight; "
        "the supplier is right. Pay the invoiced amount to the beneficiary on file.",
        target="",
        extra_fee_note="",
    )


def _payout_action(
    b: EvidenceBundle,
    kind: ActionKind,
    ev: list[Evidence],
    cfg: PolicyConfig,
    *,
    title: str,
    why: str,
    target: str,
    extra_fee_note: str,
) -> ProposedAction:
    inv = b.invoice
    assert inv is not None
    src, _est, note, ok = _funding(b, inv.amount, inv.currency, inv.transfer_method, cfg)
    ev = [*ev, Evidence(f"balance:{src}", f"{_money(b.balance(src))} {src} available")]
    if not ok:
        return ProposedAction(
            kind=ActionKind.ESCALATE,
            title=f"Escalate: not enough funds to pay {inv.invoice_id}",
            rationale=f"{why} But the wallet cannot cover it. Fund the wallet, then re-run.",
            cost_note=note,
            risk_note="Medium: the supplier deadline keeps slipping until treasury funds it.",
            risk_level="medium",
            evidence=ev,
            invoice_id=inv.invoice_id,
            target_transfer_id=target,
        )
    attempt = str(len(b.transfers) + 1)
    return ProposedAction(
        kind=kind,
        title=title,
        rationale=why,
        cost_note=note + extra_fee_note,
        risk_note="Guarded: refuses to run if any live payout for this invoice appears, if the "
        "evidence changed since approval, or if this request_id was already used.",
        risk_level="medium" if kind is ActionKind.REPLACE_PAYOUT else "low",
        evidence=ev,
        moves_money=True,
        amount=inv.amount,
        currency=inv.currency,
        source_currency=src,
        beneficiary_id=inv.beneficiary_id,
        transfer_method=inv.transfer_method,
        request_id=make_request_id(
            inv.invoice_id, kind.value, target or "first", attempt, str(inv.amount)
        ),
        target_transfer_id=target,
        invoice_id=inv.invoice_id,
    )
