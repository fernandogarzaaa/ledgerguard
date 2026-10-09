"""Deterministic stand-in for the LLM.

It speaks the same tool-calling protocol as a real model, so the agent loop is
exercised identically in tests, CI and offline demos. It reads the incident the
way a careful analyst would: find the invoice, pull its payouts, the
beneficiary, balances and FX, then submit a factual summary.
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.llm import LLMReply, ToolCall
from app.incident.invoices import parse_invoice_ids


def _called(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Map tool name -> parsed result of its most recent call."""
    names: dict[str, str] = {}
    out: dict[str, Any] = {}
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                names[tc["id"]] = tc["function"]["name"]
        elif m.get("role") == "tool":
            name = names.get(str(m.get("tool_call_id")), "")
            try:
                out[name] = json.loads(m.get("content") or "null")
            except ValueError:
                out[name] = m.get("content")
    return out


class ScriptedLLM:
    name = "scripted-analyst (no LLM)"

    def __init__(self) -> None:
        self._n = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 900,
    ) -> LLMReply:
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        done = _called(messages)
        self._n += 1

        def call(name: str, **args: Any) -> LLMReply:
            return LLMReply(tool_calls=[ToolCall(id=f"s{self._n}", name=name, arguments=args)])

        ids = parse_invoice_ids(user)
        inv_id = ids[0] if ids else ""
        if not inv_id:
            if "search_invoices" not in done:
                return call("search_invoices", text=user)
            found = done.get("search_invoices") or []
            if isinstance(found, list) and len(found) == 1:
                inv_id = str(found[0].get("invoice_id", ""))
        if not inv_id:
            return call(
                "submit_findings",
                invoice_id="",
                summary="No invoice id in the report and no single supplier match.",
            )
        if "lookup_invoice" not in done:
            return call("lookup_invoice", invoice_id=inv_id)
        if "find_invoice_transfers" not in done:
            return call("find_invoice_transfers", invoice_id=inv_id)
        inv = done.get("lookup_invoice") or {}
        if isinstance(inv, dict) and inv.get("beneficiary_id") and "get_beneficiary" not in done:
            return call("get_beneficiary", beneficiary_id=inv["beneficiary_id"])
        if "get_balances" not in done:
            return call("get_balances")
        cur = str(inv.get("amount", " ")).split(" ")[-1] if isinstance(inv, dict) else ""
        if cur and "list_fx_conversions" not in done:
            return call("list_fx_conversions", buy_currency=cur)
        return call("submit_findings", invoice_id=inv_id, summary=_summary(inv_id, done))


def _summary(inv_id: str, done: dict[str, Any]) -> str:
    inv = done.get("lookup_invoice") or {}
    ts = done.get("find_invoice_transfers") or []
    if isinstance(inv, dict) and inv.get("error"):
        return f"{inv_id} is not in the AP book."
    parts = [f"{inv_id}: {inv.get('amount')} owed to {inv.get('supplier')}."]
    if not ts:
        parts.append("No Airwallex payout references this invoice.")
    for t in ts if isinstance(ts, list) else []:
        s = f"Payout {t.get('short_reference_id') or t.get('id')} is {t.get('status')}"
        if t.get("failure_type"):
            s += f" (failure_type {t['failure_type']})"
        parts.append(s + ".")
    if isinstance(inv, dict) and inv.get("bank_details_changed_at"):
        parts.append(
            f"Vendor master shows a bank-detail change at {inv['bank_details_changed_at']}."
        )
    return " ".join(parts)
