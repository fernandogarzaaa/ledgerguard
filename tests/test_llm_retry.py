import httpx
import pytest

from app.agent.llm import OpenAICompatibleLLM, _rate_limit_wait


def test_wait_parses_provider_hint():
    r = httpx.Response(429, text='{"error":{"message":"Please try again in 1.02s."}}')
    assert _rate_limit_wait(r, 0) == pytest.approx(1.52)
    r = httpx.Response(429, text="try again in 1m2.5s")
    assert _rate_limit_wait(r, 0) == 20.0
    r = httpx.Response(429, headers={"retry-after": "3"})
    assert _rate_limit_wait(r, 0) == 3.0
    assert _rate_limit_wait(httpx.Response(429), 1) == 4.0


@pytest.mark.asyncio
async def test_chat_retries_on_429(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="Please try again in 10ms.")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    llm = OpenAICompatibleLLM("https://x/v1", "k", "m", transport=httpx.MockTransport(handler))
    reply = await llm.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "ok" and calls["n"] == 3


@pytest.mark.asyncio
async def test_salvages_final_json_sent_as_unknown_tool():
    import json as _json

    gen = _json.dumps({"name": "JSON", "arguments": {"summary": "s", "stops": []}})

    def handler(request):
        body = _json.loads(request.content)
        assert body.get("reasoning_effort") == "low"
        return httpx.Response(
            400, json={"error": {"code": "tool_use_failed", "failed_generation": gen}}
        )

    tools = [{"type": "function", "function": {"name": "plan_route"}}]
    llm = OpenAICompatibleLLM(
        "https://x/v1", "k", "openai/gpt-oss-120b", transport=httpx.MockTransport(handler)
    )
    reply = await llm.chat([{"role": "user", "content": "hi"}], tools)
    assert _json.loads(reply.content) == {"summary": "s", "stops": []}
    assert not reply.tool_calls


@pytest.mark.asyncio
async def test_real_tool_failure_still_raises():
    from app.agent.llm import LLMError

    def handler(request):
        return httpx.Response(400, json={"error": {"code": "invalid_request_error"}})

    llm = OpenAICompatibleLLM("https://x/v1", "k", "m", transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError):
        await llm.chat([{"role": "user", "content": "hi"}])
