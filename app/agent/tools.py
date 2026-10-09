"""Typed tools shared by the agent loop and the MCP server.

Read tools only look. Write tools go through the service, which enforces the
approval gate and the duplicate-payout guard; they cannot mint approvals.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.airwallex.client import AirwallexError, lookback
from app.incident.evidence import matches_invoice
from app.incident.invoices import normalise_invoice_id

if TYPE_CHECKING:
    from app.service import IncidentService

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    writes: bool = False
    agent_visible: bool = True

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def mcp_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description
            + (" [WRITE: guarded, needs a human approval token]" if self.writes else ""),
            "inputSchema": self.parameters,
        }


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


STR = {"type": "string"}


def build_tools(svc: IncidentService) -> dict[str, Tool]:
    client, book = svc.client, svc.book

    async def lookup_invoice(a: dict[str, Any]) -> Any:
        inv = book.get(str(a.get("invoice_id", "")))
        return inv.summary() if inv else {"error": "invoice not found in AP book"}

    async def search_invoices(a: dict[str, Any]) -> Any:
        return [i.summary() for i in book.find_by_supplier(str(a.get("text", "")))]

    async def find_invoice_transfers(a: dict[str, Any]) -> Any:
        inv_id = normalise_invoice_id(str(a.get("invoice_id", "")))
        ts = await client.list_transfers(from_created_at=lookback(svc.settings.lookback_days))
        return [t.summary() for t in ts if matches_invoice(t, inv_id)]

    async def get_transfer(a: dict[str, Any]) -> Any:
        return (await client.get_transfer(str(a["transfer_id"]))).summary()

    async def get_beneficiary(a: dict[str, Any]) -> Any:
        return (await client.get_beneficiary(str(a["beneficiary_id"]))).summary()

    async def list_fx_conversions(a: dict[str, Any]) -> Any:
        convs = await client.list_conversions(
            buy_currency=str(a.get("buy_currency") or "") or None,
            from_created_at=lookback(svc.settings.lookback_days),
        )
        return [c.summary() for c in convs]

    async def get_balances(a: dict[str, Any]) -> Any:
        return [b.summary() for b in await client.get_balances()]

    async def investigate_incident(a: dict[str, Any]) -> Any:
        inc = await svc.investigate(str(a["text"]), use_llm=False)
        return inc.public(brief=True)

    async def get_incident(a: dict[str, Any]) -> Any:
        inc = svc.get(str(a["incident_id"]))
        return inc.public(brief=True) if inc else {"error": "unknown incident"}

    async def execute_approved_action(a: dict[str, Any]) -> Any:
        return await svc.execute(
            str(a["incident_id"]), str(a["approval_id"]), str(a["approval_token"])
        )

    async def simulate_transfer_transition(a: dict[str, Any]) -> Any:
        t = await client.simulate_transition(
            str(a["transfer_id"]), str(a["next_status"]).upper(), a.get("failure_type")
        )
        return t.summary()

    tools = [
        Tool(
            "lookup_invoice",
            "Look up a payable invoice in the AP book: amount, currency, supplier, beneficiary_id, "
            "due date, and any recent vendor-master bank-detail change.",
            _obj({"invoice_id": STR}, ["invoice_id"]),
            lookup_invoice,
        ),
        Tool(
            "search_invoices",
            "Find invoices whose supplier name appears in free text (when no INV- id is given).",
            _obj({"text": STR}, ["text"]),
            search_invoices,
        ),
        Tool(
            "find_invoice_transfers",
            "List Airwallex payouts (transfers) that reference an invoice, with status, "
            "failure_type and request_id.",
            _obj({"invoice_id": STR}, ["invoice_id"]),
            find_invoice_transfers,
        ),
        Tool(
            "get_transfer",
            "Fetch one Airwallex transfer by id.",
            _obj({"transfer_id": STR}, ["transfer_id"]),
            get_transfer,
        ),
        Tool(
            "get_beneficiary",
            "Fetch the Airwallex beneficiary (masked bank details, account name, routing).",
            _obj({"beneficiary_id": STR}, ["beneficiary_id"]),
            get_beneficiary,
        ),
        Tool(
            "list_fx_conversions",
            "List recent Airwallex FX conversions, optionally filtered by bought currency.",
            _obj({"buy_currency": STR}),
            list_fx_conversions,
        ),
        Tool(
            "get_balances",
            "Current Airwallex wallet balances per currency.",
            _obj({}),
            get_balances,
        ),
        Tool(
            "investigate_incident",
            "Run the full incident commander on a free-text incident: timeline, classification "
            "and ONE proposed action awaiting human approval. Moves no money.",
            _obj({"text": STR}, ["text"]),
            investigate_incident,
            agent_visible=False,
        ),
        Tool(
            "get_incident",
            "Get an incident's classification, proposed action, approval status and report.",
            _obj({"incident_id": STR}, ["incident_id"]),
            get_incident,
            agent_visible=False,
        ),
        Tool(
            "execute_approved_action",
            "Execute an incident's proposed action. Requires the approval_id and the approval "
            "token a HUMAN obtained from the Ledgerguard console. Re-checks evidence, the "
            "duplicate-payout guard and idempotency before touching Airwallex.",
            _obj(
                {"incident_id": STR, "approval_id": STR, "approval_token": STR},
                ["incident_id", "approval_id", "approval_token"],
            ),
            execute_approved_action,
            writes=True,
            agent_visible=False,
        ),
        Tool(
            "simulate_transfer_transition",
            "SANDBOX/MOCK ONLY: move a transfer to PROCESSING, SENT, PAID or FAILED (with a "
            "failure_type) to stage an incident.",
            _obj(
                {"transfer_id": STR, "next_status": STR, "failure_type": STR},
                ["transfer_id", "next_status"],
            ),
            simulate_transfer_transition,
            writes=True,
            agent_visible=False,
        ),
    ]
    return {t.name: t for t in tools}


async def run_tool(tools: dict[str, Tool], name: str, args: dict[str, Any]) -> Any:
    tool = tools.get(name)
    if tool is None:
        return {"error": f"unknown tool {name}"}
    try:
        return await tool.handler(args)
    except KeyError as exc:
        return {"error": f"missing argument {exc}"}
    except AirwallexError as exc:
        return {"error": str(exc)}


def clip(obj: Any, limit: int = 3500) -> str:
    text = json.dumps(obj, default=str)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"
