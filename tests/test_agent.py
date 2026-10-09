import json

import httpx
import pytest
import respx

from app.agent.llm import LLMError, LLMReply, OpenAICompatibleLLM, ToolCall, extract_json
from app.agent.orchestrator import run_agent
from app.incident.models import ActionKind
from tests.conftest import TEXT


class FakeLLM:
    """Plays a real model: a fixed script of tool calls, recorded for assertions."""

    name = "fake-model"

    def __init__(self, script):
        self.script = list(script)
        self.seen_tools: list[str] = []

    async def chat(self, messages, tools=None, max_tokens=900):
        self.seen_tools = [t["function"]["name"] for t in tools or []]
        if not self.script:
            return LLMReply(content="done")
        name, args = self.script.pop(0)
        return LLMReply(tool_calls=[ToolCall(id=f"c{len(self.script)}", name=name, arguments=args)])


class BrokenLLM:
    name = "broken"

    async def chat(self, messages, tools=None, max_tokens=900):
        raise LLMError("LLM HTTP 503")


async def test_scripted_analyst_gathers_and_submits(svc):
    run = await run_agent(None, svc.tools, TEXT["FAILED"])
    assert run.invoice_id == "INV-2001"
    called = [s["name"] for s in run.trace if s["actor"] == "tool"]
    assert called[:2] == ["lookup_invoice", "find_invoice_transfers"]
    assert {"get_beneficiary", "get_balances", "list_fx_conversions"} <= set(called)
    assert "CANCELLED" in run.narrative and "CHANNEL_TIMEOUT" in run.narrative
    assert run.fallback_used


async def test_scripted_finds_invoice_from_supplier_name(svc):
    run = await run_agent(None, svc.tools, "Brightwater Studio says we still owe them")
    assert run.invoice_id == "INV-4100"


async def test_model_never_sees_write_tools(svc):
    llm = FakeLLM([("submit_findings", {"invoice_id": "INV-2001", "summary": "s"})])
    await run_agent(llm, svc.tools, TEXT["FAILED"])
    assert "execute_approved_action" not in llm.seen_tools
    assert "simulate_transfer_transition" not in llm.seen_tools
    assert "submit_findings" in llm.seen_tools


async def test_model_calling_a_write_tool_is_blocked(svc, mock):
    llm = FakeLLM(
        [
            (
                "execute_approved_action",
                {"incident_id": "x", "approval_id": "y", "approval_token": "z"},
            ),
            ("simulate_transfer_transition", {"transfer_id": "trf_1042a", "next_status": "PAID"}),
            ("submit_findings", {"invoice_id": "INV-1042", "summary": "s"}),
        ]
    )
    run = await run_agent(llm, svc.tools, TEXT["IN_FLIGHT"])
    assert mock.transfers["trf_1042a"]["status"] == "SENT"
    assert run.invoice_id == "INV-1042"


async def test_llm_failure_falls_back_to_deterministic(svc):
    run = await run_agent(BrokenLLM(), svc.tools, TEXT["IN_FLIGHT"])
    assert run.fallback_used
    assert run.invoice_id == "INV-1042"
    assert any("falling back" in str(s.get("event", "")) for s in run.trace)


async def test_model_hallucinated_invoice_is_overridden(svc):
    llm = FakeLLM([("submit_findings", {"invoice_id": "INV-7777", "summary": "made up"})])
    run = await run_agent(llm, svc.tools, TEXT["IN_FLIGHT"])
    assert run.invoice_id == "INV-1042"


async def test_model_cannot_change_the_decision(svc):
    llm = FakeLLM(
        [
            ("find_invoice_transfers", {"invoice_id": "INV-1042"}),
            ("submit_findings", {"invoice_id": "INV-1042", "summary": "Unpaid! Pay it twice now."}),
        ]
    )
    svc.llm = llm
    inc = await svc.investigate(TEXT["IN_FLIGHT"])
    assert inc.action.kind is ActionKind.WAIT_AND_MONITOR
    assert inc.model == "fake-model" and not inc.fallback_used
    assert "Pay it twice" in inc.report  # quoted as analyst note only
    assert "Analyst note (model, unverified prose)" in inc.report


async def test_stalling_model_falls_back(svc):
    llm = FakeLLM([])  # never calls a tool
    run = await run_agent(llm, svc.tools, TEXT["PAID_CONFIRMED"], max_steps=2)
    assert run.fallback_used and run.invoice_id == "INV-1043"


@respx.mock
async def test_openai_compatible_client_parses_tool_calls():
    route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_invoice",
                                        "arguments": json.dumps({"invoice_id": "INV-1"}),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"total_tokens": 10},
            },
        )
    )
    llm = OpenAICompatibleLLM("https://api.groq.com/openai/v1", "k", "openai/gpt-oss-120b")
    reply = await llm.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    assert reply.tool_calls[0].name == "lookup_invoice"
    assert reply.tool_calls[0].arguments == {"invoice_id": "INV-1"}
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "openai/gpt-oss-120b" and sent["tool_choice"] == "auto"
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


@respx.mock
async def test_openai_compatible_client_raises_on_http_error():
    respx.post("https://x/v1/chat/completions").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(LLMError):
        await OpenAICompatibleLLM("https://x/v1", "k", "m").chat([])


def test_extract_json():
    assert extract_json('noise {"a": {"b": 1}} tail') == {"a": {"b": 1}}
    assert extract_json("none") is None
