"""IncidentService: investigate -> propose -> approve -> execute -> report."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.agent.llm import ChatModel
from app.agent.orchestrator import run_agent
from app.agent.tools import Tool, build_tools
from app.airwallex.client import AirwallexClient, AirwallexError
from app.airwallex.mock import MockAirwallex
from app.airwallex.models import CANCELLABLE_STATUSES
from app.config import Settings
from app.incident import evidence as evidence_mod
from app.incident.approval import ApprovalError, ApprovalGate, ApprovalRequest, fingerprint
from app.incident.classifier import classify
from app.incident.evidence import EvidenceBundle, build_timeline
from app.incident.guard import DuplicatePayoutError, DuplicatePayoutGuard
from app.incident.invoices import InvoiceBook
from app.incident.models import (
    ActionKind,
    Classification,
    ProposedAction,
    TimelineEvent,
)
from app.incident.policy import PolicyConfig, propose
from app.incident.report import incident_report, supplier_message

log = logging.getLogger("ledgerguard")


@dataclass
class Incident:
    id: str
    text: str
    created_at: str
    invoice_id: str
    bundle: EvidenceBundle
    classification: Classification
    action: ProposedAction
    timeline: list[TimelineEvent]
    trace: list[dict[str, Any]]
    narrative: str
    model: str
    fallback_used: bool
    approval: ApprovalRequest
    status: str = "awaiting_approval"  # awaiting_approval | approved | executed | refused | stale
    execution: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def report(self) -> str:
        return incident_report(
            incident_id=self.id,
            text=self.text,
            b=self.bundle,
            c=self.classification,
            action=self.action,
            timeline=self.timeline,
            narrative=self.narrative,
            approval=self.approval.public() | {"approved_by": self.approval.approved_by},
            execution=self.execution,
        )

    @property
    def supplier_message(self) -> str:
        return supplier_message(self.bundle, self.action, self.execution)

    def public(self, brief: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "invoice_id": self.invoice_id,
            "invoice": self.bundle.invoice.summary() if self.bundle.invoice else None,
            "classification": self.classification.to_dict(),
            "action": self.action.to_dict(),
            "approval": self.approval.public(),
            "execution": self.execution,
            "supplier_message": self.supplier_message,
        }
        if not brief:
            out.update(
                {
                    "text": self.text,
                    "created_at": self.created_at,
                    "timeline": [e.to_dict() for e in self.timeline],
                    "trace": self.trace,
                    "narrative": self.narrative,
                    "model": self.model,
                    "fallback_used": self.fallback_used,
                    "beneficiary": self.bundle.beneficiary.summary()
                    if self.bundle.beneficiary
                    else None,
                    "transfers": [t.summary() for t in self.bundle.transfers_sorted()],
                    "balances": [b.summary() for b in self.bundle.balances],
                    "conversions": [c.summary() for c in self.bundle.conversions],
                    "report_markdown": self.report,
                    "events": self.events,
                    "data_gaps": self.bundle.errors,
                }
            )
        return out


class ExecutionRefused(RuntimeError):
    pass


class IncidentService:
    def __init__(
        self,
        settings: Settings,
        client: AirwallexClient,
        book: InvoiceBook,
        *,
        llm: ChatModel | None = None,
        guard: DuplicatePayoutGuard | None = None,
        gate: ApprovalGate | None = None,
        mock: MockAirwallex | None = None,
        policy: PolicyConfig | None = None,
        approval_pin: str = "",
    ) -> None:
        self.settings = settings
        self.client = client
        self.book = book
        self.llm = llm
        self.mock = mock
        self.policy = policy or PolicyConfig()
        self.guard = guard or DuplicatePayoutGuard(settings.ledger_path or None)
        self.gate = gate or ApprovalGate(
            settings.approval_secret, settings.approval_ttl_s, pin=approval_pin
        )
        self.incidents: dict[str, Incident] = {}
        self.tools: dict[str, Tool] = build_tools(self)
        self.seen_events: set[str] = set()

    @property
    def mode(self) -> str:
        return "mock" if self.mock is not None else "sandbox"

    def get(self, incident_id: str) -> Incident | None:
        return self.incidents.get(incident_id)

    # -- investigate -------------------------------------------------------------
    async def _decide(
        self, invoice_id: str, text: str
    ) -> tuple[EvidenceBundle, Classification, ProposedAction, list[TimelineEvent]]:
        bundle = await evidence_mod.collect(
            self.client, self.book, invoice_id, lookback_days=self.settings.lookback_days
        )
        cls = classify(bundle)
        action = propose(bundle, cls, self.policy)
        action.evidence_digest = fingerprint(bundle.snapshot())[:16]
        return bundle, cls, action, build_timeline(bundle, text)

    async def investigate(self, text: str, *, use_llm: bool = True) -> Incident:
        text = (text or "").strip()
        if not text:
            raise ValueError("describe the incident")
        started = time.perf_counter()
        run = await run_agent(
            self.llm if use_llm else None, self.tools, text, self.settings.agent_max_steps
        )
        invoice_id = run.invoice_id or "UNKNOWN"
        bundle, cls, action, timeline = await self._decide(invoice_id, text)
        trace = [
            *run.trace,
            {
                "actor": "code",
                "event": "collected evidence and decided",
                "case": cls.case.value,
                "action": action.kind.value,
                "airwallex_calls": len(self.client.calls),
                "ms": round((time.perf_counter() - started) * 1000, 1),
            },
        ]
        inc_id = f"inc_{datetime.now(UTC):%m%d}_{secrets.token_hex(3)}"
        approval = self.gate.request(inc_id, action.binding())
        inc = Incident(
            id=inc_id,
            text=text,
            created_at=datetime.now(UTC).isoformat(),
            invoice_id=invoice_id,
            bundle=bundle,
            classification=cls,
            action=action,
            timeline=timeline,
            trace=trace,
            narrative=run.narrative,
            model=run.model,
            fallback_used=run.fallback_used,
            approval=approval,
        )
        self.incidents[inc_id] = inc
        return inc

    # -- approve -----------------------------------------------------------------
    def approve(self, incident_id: str, approver: str, pin: str = "") -> dict[str, Any]:
        inc = self._require(incident_id)
        token = self.gate.approve(inc.approval.approval_id, approver, pin)
        inc.status = "approved"
        inc.events.append({"at": datetime.now(UTC).isoformat(), "event": f"approved by {approver}"})
        return {
            "incident_id": inc.id,
            "approval_id": inc.approval.approval_id,
            "approval_token": token,
            "fingerprint": inc.approval.fingerprint,
        }

    def _require(self, incident_id: str) -> Incident:
        inc = self.incidents.get(incident_id)
        if inc is None:
            raise KeyError(f"unknown incident {incident_id}")
        return inc

    # -- execute -----------------------------------------------------------------
    async def execute(self, incident_id: str, approval_id: str, token: str) -> dict[str, Any]:
        inc = self._require(incident_id)
        if approval_id != inc.approval.approval_id:
            return self._refuse(inc, "approval does not belong to this incident")
        # Re-read Airwallex now. The action must be the same one the human approved.
        bundle, cls, fresh, timeline = await self._decide(inc.invoice_id, inc.text)
        try:
            self.gate.consume(approval_id, token, fresh.binding())
        except ApprovalError as exc:
            if "changed" in str(exc):
                inc.status = "stale"
                inc.bundle, inc.classification, inc.action, inc.timeline = (
                    bundle,
                    cls,
                    fresh,
                    timeline,
                )
                inc.approval = self.gate.request(inc.id, fresh.binding())
                inc.execution = {
                    "outcome": "refused",
                    "message": f"{exc}. New proposal: {fresh.kind.value}.",
                }
                return {"incident": inc.public(), **inc.execution}
            return self._refuse(inc, str(exc))

        action = inc.action
        try:
            if action.kind in (ActionKind.REPLACE_PAYOUT, ActionKind.PAY_INVOICE):
                result = await self._payout(inc, bundle)
            elif action.kind is ActionKind.CANCEL_DUPLICATE:
                result = await self._cancel_duplicate(inc, bundle)
            else:
                result = {
                    "outcome": "recorded",
                    "message": f"{action.kind.value} recorded; no money moved. Send the supplier "
                    "message below.",
                }
        except (DuplicatePayoutError, ExecutionRefused) as exc:
            return self._refuse(inc, str(exc))
        except AirwallexError as exc:
            return self._refuse(inc, f"Airwallex rejected the call: {exc}")

        inc.execution = result
        inc.status = "executed"
        if inc.action.moves_money:
            # Show the post-execution state (new/cancelled transfer) on the timeline.
            bundle = await evidence_mod.collect(
                self.client, self.book, inc.invoice_id, lookback_days=self.settings.lookback_days
            )
            timeline = build_timeline(bundle, inc.text)
        inc.bundle, inc.timeline = bundle, timeline
        inc.events.append(
            {"at": datetime.now(UTC).isoformat(), "event": f"executed: {result['outcome']}"}
        )
        return {"incident": inc.public(), **result}

    def _refuse(self, inc: Incident, reason: str) -> dict[str, Any]:
        refusal = {"outcome": "refused", "message": reason}
        if inc.status != "executed":
            # Keep a successful execution on record; a refused replay only logs an event.
            inc.execution = refusal
            inc.status = "refused"
        inc.events.append({"at": datetime.now(UTC).isoformat(), "event": f"refused: {reason}"})
        return {"incident": inc.public(), **refusal}

    async def _payout(self, inc: Incident, bundle: EvidenceBundle) -> dict[str, Any]:
        a = inc.action
        assert a.amount is not None
        lock = self.guard.acquire(a.invoice_id, a.request_id, bundle.transfers, incident_id=inc.id)
        payload: dict[str, Any] = {
            "request_id": a.request_id,
            "beneficiary_id": a.beneficiary_id,
            "transfer_amount": str(a.amount),
            "transfer_currency": a.currency,
            "source_currency": a.source_currency or a.currency,
            "transfer_method": a.transfer_method or "LOCAL",
            "reference": a.invoice_id,
            "reason": "payment_to_supplier",
            "remarks": f"Ledgerguard {inc.id} {a.kind.value}",
            "metadata": {
                "invoice_id": a.invoice_id,
                "incident_id": inc.id,
                "replaces": a.target_transfer_id or "",
            },
        }
        try:
            transfer, created_now = await self.client.create_transfer(payload)
        except AirwallexError as exc:
            if not exc.is_ambiguous:
                self.guard.abort(a.invoice_id, a.request_id, f"{exc.code}")
            raise
        self.guard.confirm(a.invoice_id, a.request_id, transfer)
        msg = (
            f"Created transfer {transfer.short_reference_id or transfer.id} "
            f"({transfer.status}) with request_id {a.request_id}."
            if created_now
            else f"Airwallex already had request_id {a.request_id}: returned the existing "
            f"transfer {transfer.id} instead of creating a second one."
        )
        return {
            "outcome": "transfer_created" if created_now else "idempotent_replay",
            "message": msg,
            "transfer": transfer.summary(),
            "lock": {"state": lock.state, "request_id": lock.request_id},
        }

    async def _cancel_duplicate(self, inc: Incident, bundle: EvidenceBundle) -> dict[str, Any]:
        a = inc.action
        target = next((t for t in bundle.transfers if t.id == a.target_transfer_id), None)
        if target is None or target.status not in CANCELLABLE_STATUSES:
            raise ExecutionRefused(
                f"transfer {a.target_transfer_id} is no longer cancellable "
                f"({target.status if target else 'missing'})"
            )
        others = [t for t in bundle.transfers if t.id != target.id and t.is_live]
        if not others:
            raise ExecutionRefused(
                "refused: cancelling would leave the invoice unpaid (no other live payout)"
            )
        cancelled = await self.client.cancel_transfer(target.id)
        return {
            "outcome": "duplicate_cancelled",
            "message": f"Cancelled duplicate {target.short_reference_id or target.id}; kept "
            f"{others[0].short_reference_id or others[0].id} ({others[0].status}).",
            "transfer": cancelled.summary(),
        }

    # -- webhooks ----------------------------------------------------------------
    async def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """payout.transfer.* events: dedupe on id, re-read state, refresh incidents."""
        eid = str(event.get("id") or "")
        if eid and eid in self.seen_events:
            return {"status": "duplicate", "id": eid}
        if eid:
            self.seen_events.add(eid)
        name = str(event.get("name") or "")
        obj = (event.get("data") or {}).get("object") or {}
        tid = str(obj.get("id") or "")
        touched: list[str] = []
        if name.startswith("payout.transfer.") and tid:
            for inc in self.incidents.values():
                if any(t.id == tid for t in inc.bundle.transfers) or (
                    inc.execution and (inc.execution.get("transfer") or {}).get("id") == tid
                ):
                    bundle, cls, action, timeline = await self._decide(inc.invoice_id, inc.text)
                    inc.bundle, inc.timeline = bundle, timeline
                    inc.events.append(
                        {
                            "at": datetime.now(UTC).isoformat(),
                            "event": f"webhook {name} for {tid}; now {cls.case.value}",
                        }
                    )
                    if (
                        inc.status == "awaiting_approval"
                        and action.binding() != inc.action.binding()
                    ):
                        inc.classification, inc.action = cls, action
                        inc.approval = self.gate.request(inc.id, action.binding())
                    touched.append(inc.id)
        return {"status": "processed", "id": eid, "name": name, "incidents": touched}
