"""Incident report (markdown) and the draft supplier message.

Both are built from facts in code. The model's narrative, when there is one,
is quoted as the analyst note and never supplies an amount, date or reference.
"""

from __future__ import annotations

from typing import Any

from app.airwallex.models import fmt
from app.incident.evidence import EvidenceBundle
from app.incident.models import ActionKind, Classification, ProposedAction, TimelineEvent


def _t(bundle: EvidenceBundle, transfer_id: str) -> Any:
    return next((t for t in bundle.transfers if t.id == transfer_id), None)


def supplier_message(
    b: EvidenceBundle, action: ProposedAction, execution: dict[str, Any] | None = None
) -> str:
    inv = b.invoice
    if inv is None:
        return (
            f"Hello,\n\nThanks for flagging {b.invoice_id}. We cannot find this invoice in our "
            "payables yet. Could you send a copy of the invoice and the PO it relates to? We will "
            "confirm the status within one business day.\n\nKind regards,\nAccounts Payable"
        )
    greet = f"Hello {inv.supplier} team,\n\n"
    sign = "\n\nKind regards,\nAccounts Payable"
    head = f"Thank you for raising invoice {inv.invoice_id} ({inv.amount} {inv.currency}). "
    t = _t(b, action.target_transfer_id)
    ref = (t.short_reference_id or t.id) if t is not None else ""
    k = action.kind
    if k is ActionKind.SEND_PROOF_OF_PAYMENT and t is not None:
        when = f"{t.updated_at:%d %b %Y}" if t.updated_at else "recently"
        body = (
            f"Our records show this invoice was paid in full on {when}: {fmt(t.transfer_amount)} "
            f'{t.transfer_currency}, payment reference "{t.reference}", Airwallex reference '
            f"{ref}. Could you check with your bank using that reference? If it still does not "
            "appear, reply with your bank statement for that date and we will trace it."
        )
    elif k is ActionKind.WAIT_AND_MONITOR and t is not None:
        body = (
            f"The payment is on its way: it left us as Airwallex reference {ref} "
            f'({fmt(t.transfer_amount)} {t.transfer_currency}, reference "{t.reference}") and is '
            f"currently {t.status}. Bank transfers can take a little longer to land; we are "
            "monitoring it and will update you as soon as it is confirmed. Please do not treat "
            "this invoice as unpaid in the meantime."
        )
    elif k is ActionKind.REPLACE_PAYOUT:
        sent = (execution or {}).get("transfer") or {}
        new_ref = sent.get("short_reference_id") or sent.get("id") or "(pending)"
        body = (
            f"Our first payment ({ref}) did not complete and the funds came back to us. "
            + (
                f"We have sent a replacement payment, Airwallex reference {new_ref}, for "
                f"{inv.amount} {inv.currency}. "
                if execution
                else f"We are sending a replacement payment for {inv.amount} {inv.currency} today. "
            )
            + "Only this replacement will reach you; the first payment will not arrive later."
        )
    elif k is ActionKind.PAY_INVOICE:
        sent = (execution or {}).get("transfer") or {}
        new_ref = sent.get("short_reference_id") or sent.get("id")
        body = "You are right, this invoice had not been paid, and we apologise for the delay. " + (
            f"Payment has now been sent, Airwallex reference {new_ref}."
            if new_ref
            else "We are releasing the payment today and will send the reference."
        )
    elif k is ActionKind.CANCEL_DUPLICATE:
        keep = next((x for x in b.transfers if x.is_paid or x.status == "SENT"), None)
        kref = (keep.short_reference_id or keep.id) if keep is not None else ""
        body = (
            f"This invoice was paid under Airwallex reference {kref}. A second payment was "
            "queued in error and has been stopped, so you will receive the amount once, not twice."
        )
    elif k is ActionKind.REQUEST_BANK_CONFIRMATION:
        body = (
            f"Our payment ({ref}) was returned to us by your bank. Before we send it again, could "
            "you check with your bank why it was returned and confirm the account is open and "
            "able to receive this payment? As soon as you confirm, we will resend."
        )
    elif k is ActionKind.VERIFY_BENEFICIARY:
        body = (
            "Before we send this payment we need to verify your bank details. We will call you on "
            "the number we already hold to confirm them. For your security, we never act on bank "
            "detail changes received by email alone."
        )
    else:
        body = (
            "We are looking into this with our payment provider and will come back to you with "
            "an update within one business day."
        )
    return greet + head + body + sign


def incident_report(
    *,
    incident_id: str,
    text: str,
    b: EvidenceBundle,
    c: Classification,
    action: ProposedAction,
    timeline: list[TimelineEvent],
    narrative: str = "",
    approval: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
) -> str:
    inv = b.invoice
    lines = [f"# Incident {incident_id}: {b.invoice_id}", ""]
    lines += ["## Report", "", f"> {text.strip()}", ""]
    if inv:
        lines += [
            f"- Invoice: **{inv.invoice_id}**, {inv.amount} {inv.currency} to {inv.supplier} "
            f"(due {inv.due_date or '?'}, {inv.transfer_method})",
        ]
    lines += [f"- Classification: **{c.case.value}**"]
    lines += [f"  - {r}" for r in c.reasons]
    if c.flags:
        lines += [f"- Flags: {', '.join(c.flags)}"]
    lines += ["", "## Timeline (UTC)", "", "| When | What | Detail |", "|---|---|---|"]
    for e in timeline:
        lines.append(f"| {e.at[:16].replace('T', ' ')} | {e.title} | {e.detail} |")
    lines += ["", "## Decision", "", f"**{action.title}** (`{action.kind.value}`)", ""]
    lines += [action.rationale, "", f"- Cost: {action.cost_note}"]
    lines += [f"- Risk ({action.risk_level}): {action.risk_note}"]
    if action.moves_money:
        lines += [
            f"- Payload binding: {action.amount} {action.currency} from {action.source_currency} "
            f"to beneficiary {action.beneficiary_id} via {action.transfer_method or '-'}, "
            f"request_id `{action.request_id}`"
            if action.kind is not ActionKind.CANCEL_DUPLICATE
            else f"- Cancels transfer `{action.target_transfer_id}`"
        ]
    lines += ["", "### Evidence", ""]
    lines += [f"- `{e.ref}`: {e.claim}" for e in action.evidence]
    if narrative:
        lines += ["", "### Analyst note (model, unverified prose)", "", narrative.strip()]
    lines += ["", "## Approval", ""]
    if approval and approval.get("approved_by"):
        lines += [
            f"- Approved by **{approval['approved_by']}**; fingerprint `{approval['fingerprint']}`"
        ]
    else:
        lines += ["- Pending human approval."]
    lines += ["", "## Execution", ""]
    if execution:
        lines += [f"- Outcome: **{execution.get('outcome')}**", f"- {execution.get('message', '')}"]
        tr = execution.get("transfer")
        if isinstance(tr, dict):
            lines += [
                f"- Transfer `{tr.get('id')}` {tr.get('status')} {tr.get('amount')} "
                f"(request_id `{tr.get('request_id')}`)"
            ]
    else:
        lines += ["- Not executed yet."]
    original = _t(b, action.target_transfer_id)
    if original is not None:
        lines += [
            "",
            "## Original transfer",
            "",
            f"- `{original.id}` ({original.short_reference_id}) is **{original.status}**"
            + (f", failure_type {original.failure_type}" if original.failure_type else "")
            + ". It is recorded here and is not reused.",
        ]
    if b.errors:
        lines += ["", "## Data gaps", ""] + [f"- {e}" for e in b.errors]
    return "\n".join(lines) + "\n"
