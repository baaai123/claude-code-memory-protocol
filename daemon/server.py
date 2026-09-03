"""常驻 memory daemon — HTTP transport + 精简 REST 端点.

Claude Code hooks (gate/weave/ingest) bypass the full MCP handshake and call
these REST endpoints; the Claude Code memory MCP server points at /mcp via
.mcp.json.

Architecture:
  - Reuses memory_skill's low-level ``Server`` (not FastMCP) so tool
    registration logic is not duplicated
  - ``StreamableHTTPSessionManager`` exposes it as a streamable-HTTP MCP
    endpoint (agent path: ProtocolGate enforced)
  - REST /weave uses weaver.weave() directly without ProtocolGate so hooks
    can inject memory context every user turn (hook path: gate.py enforces
    the turn protocol instead)

Deps: starlette + uvicorn + mcp (all present in the memory-skill venv).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from memory_skill.contracts import MemorySkillConfig
from memory_skill.mcp_tools import TOOLS, ToolHandler
from memory_skill.skill import MemorySkill

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude-code-memory-protocol")


def _db_path() -> str:
    return os.environ.get(
        "MEMORY_SKILL_DB_PATH",
        os.path.join(STATE_DIR, "memory.db"),
    )


# ── MemorySkill lifecycle ────────────────────────────────────────────────
_skill: MemorySkill | None = None
_handler: ToolHandler | None = None


def _ensure_skill() -> MemorySkill:
    global _skill, _handler
    if _skill is None:
        import time as _time

        t0 = _time.monotonic()
        os.makedirs(STATE_DIR, exist_ok=True)
        db = _db_path()
        os.makedirs(os.path.dirname(db) or ".", exist_ok=True)
        _skill = MemorySkill(MemorySkillConfig(db_path=db))
        _handler = ToolHandler(_skill)
        print(
            f"[memory daemon] MemorySkill loaded in {_time.monotonic() - t0:.1f}s "
            f"db={_db_path()}",
            flush=True,
        )
    return _skill


def _handle_sync(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return _handler.handle(name, args)


def _weave_direct(user_message: str) -> dict:
    """Weave without ProtocolGate (hook path).

    ProtocolGate is an agent-session concern (classify before weave). Hooks
    run once per user turn to inject context, so they must not be blocked by
    pending classifications from the agent path. gate.py enforces the turn
    protocol separately; MCP tool calls keep the full gate.
    """
    from memory_skill.weaver import WeaverStores, weave

    skill = _ensure_skill()
    stores = WeaverStores(
        saw_buffer=skill.saw_buffer,
        dialogue_store=skill.dialogue_store,
        learned_store=skill.learned_store,
        retriever=skill.retriever,
        agent_name=skill.config.agent_name,
        namespace=skill.config.namespace,
        tree=skill.tree,
        gaps=skill.gaps,
        pending_store=getattr(skill, "pending_store", None),
        mission_store=skill._ensure_mission_store(),
        degraded=skill.embedder.degraded,
        degraded_reason=skill.embedder.reason,
        character=getattr(skill, "character", None),
        protocol=skill.protocol,
        learning_queue=getattr(skill, "learning_queue", None),
    )
    ctx = weave(stores, user_message)
    return {
        "tier1_context": ctx.tier1_context,
        "tier2_context": ctx.tier2_context,
        "memory_nudge": ctx.memory_nudge,
        "prompt_block": ctx.to_prompt_block(),
        "is_empty": ctx.is_empty,
    }


# ── MCP Server construction ──────────────────────────────────────────────
async def _build_mcp_server() -> Any:
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    skill = _ensure_skill()
    handler = ToolHandler(skill)
    server = Server(name="memory-skill", version="4.0.0")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"])
            for t in TOOLS
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, handler.handle, name, arguments)
        return [
            TextContent(
                type="text",
                text=json.dumps(result, indent=2, ensure_ascii=False, default=str),
            )
        ]

    return server


_session_manager: StreamableHTTPSessionManager | None = None


async def _get_session_manager() -> StreamableHTTPSessionManager:
    global _session_manager
    if _session_manager is None:
        mcp_server = await _build_mcp_server()
        _session_manager = StreamableHTTPSessionManager(
            app=mcp_server,
            json_response=True,
            stateless=True,
        )
    return _session_manager


async def handle_mcp(scope: Any, receive: Any, send: Any) -> None:
    sm = await _get_session_manager()
    await sm.handle_request(scope, receive, send)


# ── REST hooks (Claude Code hook path) ──────────────────────────────────
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "mcp_endpoint": "/mcp", "db": _db_path()})


async def do_weave(request: Request) -> JSONResponse:
    body = await request.json()
    result = await asyncio.to_thread(
        _weave_direct,
        body.get("user_message", ""),
    )
    return JSONResponse(result)


async def do_ingest(request: Request) -> JSONResponse:
    body = await request.json()
    result = await asyncio.to_thread(
        _handle_sync,
        "memory_ingest",
        {"content": body.get("content", ""), "role": body.get("role", "user")},
    )
    return JSONResponse(result)


async def do_classify(request: Request) -> JSONResponse:
    body = await request.json()
    result = await asyncio.to_thread(
        _handle_sync,
        "memory_classify",
        {"category": body.get("category", "chat"), "note": body.get("note", "")},
    )
    return JSONResponse(result)


async def do_status(request: Request) -> JSONResponse:
    result = await asyncio.to_thread(_handle_sync, "memory_status", {})
    return JSONResponse(result)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    sm = await _get_session_manager()
    await asyncio.to_thread(_ensure_skill)  # warm the embedder at startup
    async with sm.run():
        yield


# REST routes first; MCP mount("/") last (Starlette matches in order).
# Mount("/mcp") with a bare ASGI target 307-redirects to /mcp/, which the
# python MCP client does not follow. mount("/") leaves the full path for
# handle_mcp, so the endpoint stays clean at /mcp.
routes = [
    Route("/health", health),
    Route("/weave", do_weave, methods=["POST"]),
    Route("/ingest", do_ingest, methods=["POST"]),
    Route("/classify", do_classify, methods=["POST"]),
    Route("/status", do_status),
    Mount("/", handle_mcp),
]

app = Starlette(routes=routes, lifespan=lifespan)
