#!/usr/bin/env python3
"""PreToolUse hook — force memory_weave before any non-memory tool.

Mirrors dsh `tools/pre-execute`. State is managed by lib/state.py:
weave_inject.py (UserPromptSubmit) resets the gate each turn; calling
memory_weave marks it consulted.

Memory tools (mcp__opencode_memory__*) always pass. Other tools are denied
while the gate is pending. Deny is unbypassable (works even with
--dangerously-skip-permissions).

Fails open: no state file / parse error / malformed input → allow through.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from lib import state  # noqa: E402


def is_memory_tool(name: str) -> bool:
    return "memory" in name.lower()


def main() -> None:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except ValueError:
        return

    session_id = event.get("session_id") or "unknown"
    tool_name = event.get("tool_name") or ""

    if is_memory_tool(tool_name):
        if "memory_weave" in tool_name:
            state.mark_weaved(session_id)
        return

    if state.is_weaved(session_id):
        return

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Memory protocol: call mcp__opencode_memory__memory_weave "
                f"first, then retry {tool_name}. Every turn must consult memory "
                f"before acting."
            ),
        }
    }
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
