"""Provider-agnostic OpenAI-compatible chat client (Groq by default).

Works with any ``/chat/completions`` endpoint that supports ``tools``:
OpenRouter, Nebius Token Factory, OpenAI, Groq, a local vLLM/llama.cpp server.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMReply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    def as_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in self.tool_calls
            ]
        return msg


class ChatModel(Protocol):
    @property
    def name(self) -> str: ...

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 900,
    ) -> LLMReply: ...


class LLMError(RuntimeError):
    pass


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass
class OpenAICompatibleLLM:
    base_url: str
    api_key: str
    model: str
    timeout_s: float = 60.0
    transport: httpx.AsyncBaseTransport | None = None
    temperature: float = 0.2
    max_retries: int = 4

    @property
    def name(self) -> str:
        return self.model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 900,
    ) -> LLMReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        if "gpt-oss" in self.model:
            # Reasoning models spend max_tokens on hidden reasoning first; keep it short
            # so the visible answer is not truncated to an empty string.
            body["reasoning_effort"] = "low"
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if "openrouter.ai" in self.base_url:
            headers["HTTP-Referer"] = "https://github.com/fernandogarzaaa/ledgerguard"
            headers["X-Title"] = "Ledgerguard"
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as http:
            for attempt in range(self.max_retries + 1):
                resp = await http.post(
                    f"{self.base_url}/chat/completions", json=body, headers=headers
                )
                if resp.status_code != 429 or attempt == self.max_retries:
                    break
                await asyncio.sleep(_rate_limit_wait(resp, attempt))
        salvaged = _salvage_tool_use_failure(resp, tools)
        if salvaged is not None:
            return salvaged
        if resp.status_code >= 400:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected LLM response: {str(data)[:300]}") from exc
        calls = [
            ToolCall(
                id=str(tc.get("id") or f"call_{i}"),
                name=str(tc.get("function", {}).get("name", "")),
                arguments=_parse_args(tc.get("function", {}).get("arguments")),
            )
            for i, tc in enumerate(message.get("tool_calls") or [])
        ]
        return LLMReply(
            content=message.get("content") or "", tool_calls=calls, usage=data.get("usage") or {}
        )


def _salvage_tool_use_failure(
    resp: httpx.Response, tools: list[dict[str, Any]] | None
) -> LLMReply | None:
    """Recover a final answer the model wrongly emitted as a call to a non-existent tool.

    Groq rejects such generations with 400 ``tool_use_failed`` and returns the text in
    ``failed_generation``; reasoning models sometimes wrap their final JSON that way.
    """
    if resp.status_code != 400:
        return None
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        return None
    if err.get("code") != "tool_use_failed" or not err.get("failed_generation"):
        return None
    known = {t.get("function", {}).get("name") for t in tools or []}
    payload = extract_json(str(err["failed_generation"]))
    if not payload or payload.get("name") in known:
        return None
    args = payload.get("arguments")
    if isinstance(args, str):
        args = _parse_args(args)
    if not isinstance(args, dict) or not args:
        return None
    return LLMReply(content=json.dumps(args))


_TRY_AGAIN = re.compile(r"try again in\s+(?:(\d+)m)?([\d.]+)(ms|s)", re.IGNORECASE)


def _rate_limit_wait(resp: httpx.Response, attempt: int, cap: float = 20.0) -> float:
    """Seconds to wait after a 429: Retry-After header, provider hint, or backoff."""
    header = resp.headers.get("retry-after")
    if header:
        try:
            return min(max(float(header), 0.5), cap)
        except ValueError:
            pass
    match = _TRY_AGAIN.search(resp.text or "")
    if match:
        minutes = float(match.group(1) or 0)
        value = float(match.group(2))
        secs = minutes * 60 + (value / 1000 if match.group(3).lower() == "ms" else value)
        return min(secs + 0.5, cap)
    return min(2.0 * (2**attempt), cap)


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply (fenced or bare)."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                    except ValueError:
                        break
                    return obj if isinstance(obj, dict) else None
        start = text.find("{", start + 1)
    return None
