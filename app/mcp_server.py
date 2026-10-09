"""Minimal MCP server (JSON-RPC 2.0) for Ledgerguard, dependency-free.

Transports:
- stdio: ``python -m app.mcp_server`` (one JSON-RPC message per line), for
  Claude Code / Cursor: ``claude mcp add ledgerguard -- python -m app.mcp_server``
- HTTP: ``POST /mcp`` on the web app (JSON response, MCP streamable-HTTP style).

Exposes the read tools, ``investigate_incident``/``get_incident``, and the
guarded write tools. There is deliberately no approve tool: an approval token
can only be minted by a human in the console (or ``POST /api/incidents/{id}/approve``).
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from app import __version__
from app.agent.tools import run_tool
from app.service import IncidentService

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}


def _result(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


async def handle_message(svc: IncidentService, msg: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid request")
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg
    if method == "initialize":
        requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        return _result(
            mid,
            {
                "protocolVersion": requested
                if requested in SUPPORTED_VERSIONS
                else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ledgerguard", "version": __version__},
                "instructions": "Payment-ops incident commander for Airwallex. Investigate with "
                "investigate_incident; money only moves via execute_approved_action with a "
                "human-issued approval token.",
            },
        )
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [t.mcp_schema() for t in svc.tools.values()]})
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        tool = svc.tools.get(name)
        if tool is None:
            return _error(mid, -32602, f"unknown tool {name}")
        if name == "simulate_transfer_transition" and svc.mode not in ("mock", "sandbox"):
            return _error(mid, -32602, "simulation is sandbox-only")
        try:
            out = await run_tool(svc.tools, name, args if isinstance(args, dict) else {})
        except (ValueError, KeyError) as exc:
            out = {"error": str(exc)}
        is_error = isinstance(out, dict) and "error" in out
        if isinstance(out, dict) and out.get("outcome") == "refused":
            is_error = True
        return _result(
            mid,
            {
                "content": [{"type": "text", "text": json.dumps(out, default=str, indent=1)}],
                "structuredContent": out if isinstance(out, dict) else {"items": out},
                "isError": is_error,
            },
        )
    if is_notification:
        return None
    return _error(mid, -32601, f"method not found: {method}")


async def serve_stdio(svc: IncidentService) -> None:  # pragma: no cover - exercised manually
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = _error(None, -32700, "parse error")
        else:
            reply = await handle_message(svc, msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, default=str) + "\n")
            sys.stdout.flush()


def main() -> None:  # pragma: no cover
    from app.main import build_service

    svc = build_service()
    asyncio.run(serve_stdio(svc))


if __name__ == "__main__":  # pragma: no cover
    main()
