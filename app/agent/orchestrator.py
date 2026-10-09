"""The agent loop: the model reads and gathers; the code decides.

The model gets read-only tools plus ``submit_findings``. It never sees a write
tool. Whatever it concludes, classification and the proposed action come from
``app.incident.classifier`` / ``policy`` over evidence the code collects itself.
If the model errors or stalls, the deterministic analyst takes over and the
trace says so.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.agent.llm import ChatModel, LLMError
from app.agent.scripted import ScriptedLLM
from app.agent.tools import Tool, clip, run_tool
from app.incident.invoices import normalise_invoice_id, parse_invoice_ids

SYSTEM_PROMPT = """You are Ledgerguard, a payment-operations analyst on an Airwallex account.
A supplier says an invoice was not paid. Use the tools to gather facts: look up the invoice,
list its Airwallex payouts, read the beneficiary, balances and any FX conversions.
Rules:
- You cannot move money and you do not decide the action; deterministic code does that.
- Never invent ids, amounts, dates or statuses. Only repeat what tools returned.
- In Airwallex a failed transfer ends CANCELLED; that does not mean a person cancelled it.
- SENT and PROCESSING are in flight, not final. PAID can still fail later on a bank return.
When you have the facts, call submit_findings once with the invoice_id and a 2-4 sentence
factual summary citing payout references and statuses."""

SUBMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_findings",
        "description": "Finish: report the invoice id and a short factual summary.",
        "parameters": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["invoice_id", "summary"],
        },
    },
}


@dataclass
class AgentRun:
    invoice_id: str
    narrative: str
    model: str
    fallback_used: bool
    trace: list[dict[str, Any]] = field(default_factory=list)


async def _loop(
    llm: ChatModel, tools: dict[str, Tool], text: str, max_steps: int, trace: list[dict[str, Any]]
) -> tuple[str, str] | None:
    visible = [t.openai_schema() for t in tools.values() if t.agent_visible and not t.writes]
    visible.append(SUBMIT_TOOL)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    for step in range(max_steps):
        t0 = time.perf_counter()
        reply = await llm.chat(messages, tools=visible, max_tokens=1200)
        trace.append(
            {
                "step": step,
                "actor": "model",
                "model": llm.name,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "content": (reply.content or "")[:400],
                "tool_calls": [{"name": c.name, "args": c.arguments} for c in reply.tool_calls],
            }
        )
        if not reply.tool_calls:
            messages.append(reply.as_message())
            messages.append(
                {"role": "user", "content": "Call submit_findings now with what you have."}
            )
            continue
        messages.append(reply.as_message())
        for call in reply.tool_calls:
            if call.name == "submit_findings":
                return (
                    str(call.arguments.get("invoice_id") or ""),
                    str(call.arguments.get("summary") or ""),
                )
            tool = tools.get(call.name)
            if tool is None or tool.writes or not tool.agent_visible:
                result: Any = {"error": f"tool {call.name} is not available to the agent"}
            else:
                t1 = time.perf_counter()
                result = await run_tool(tools, call.name, call.arguments)
                trace.append(
                    {
                        "step": step,
                        "actor": "tool",
                        "name": call.name,
                        "args": call.arguments,
                        "ms": round((time.perf_counter() - t1) * 1000, 1),
                        "result": clip(result, 600),
                    }
                )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": clip(result)})
    return None


async def run_agent(
    llm: ChatModel | None, tools: dict[str, Tool], text: str, max_steps: int = 10
) -> AgentRun:
    trace: list[dict[str, Any]] = []
    model = llm or ScriptedLLM()
    fallback = llm is None
    outcome: tuple[str, str] | None = None
    try:
        outcome = await _loop(model, tools, text, max_steps, trace)
    except (LLMError, httpx.HTTPError, OSError, ValueError, KeyError) as exc:
        trace.append({"actor": "system", "event": f"model failed: {exc}"[:300]})
        outcome = None
    if outcome is None and not fallback:
        trace.append({"actor": "system", "event": "falling back to the deterministic analyst"})
        fallback = True
        model = ScriptedLLM()
        outcome = await _loop(model, tools, text, max_steps, trace)
    inv_id, narrative = outcome or ("", "")
    ids = parse_invoice_ids(text)
    if inv_id:
        inv_id = normalise_invoice_id(inv_id)
        if ids and inv_id not in ids:
            trace.append(
                {
                    "actor": "system",
                    "event": f"model named {inv_id} but the report says {ids[0]}; using the report",
                }
            )
            inv_id = ids[0]
    elif ids:
        inv_id = ids[0]
    return AgentRun(
        invoice_id=inv_id,
        narrative=narrative,
        model=model.name,
        fallback_used=fallback,
        trace=trace,
    )
