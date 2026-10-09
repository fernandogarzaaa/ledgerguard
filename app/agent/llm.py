"""Provider-agnostic OpenAI-compatible chat client (Groq by default).

Works with any ``/chat/completions`` endpoint that supports ``tools``:
OpenRouter, Nebius Token Factory, OpenAI, Groq, a local vLLM/llama.cpp server.
"""

from __future__ import annotations

import json
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
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if "openrouter.ai" in self.base_url:
            headers["HTTP-Referer"] = "https://github.com/fernandogarzaaa/ledgerguard"
            headers["X-Title"] = "Ledgerguard"
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as http:
            resp = await http.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
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
