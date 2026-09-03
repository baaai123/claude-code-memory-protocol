#!/usr/bin/env python3
"""UserPromptSubmit hook — start a turn: reset gate + inject weave context.

Mirrors dsh `agent/pre-step`:
  1. Reset the weave gate (fresh user prompt = new turn).
  2. Ask the daemon for weave context for this prompt.
  3. Inject it as additionalContext so the model sees memory before acting.

Fails open: any daemon/weave error → no context injected, prompt proceeds.
Output shape (must nest under hookSpecificOutput, top-level is ignored):
  {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                          "additionalContext": "..."}}
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from lib import daemon_client  # noqa: E402
from lib import state  # noqa: E402

MAX_CTX_CHARS = int(os.environ.get("CCMP_MAX_CONTEXT_CHARS", "6000"))


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2):]
    return f"{head}\n…[truncated {len(text) - limit} chars]…\n{tail}"


def _render_context(weave: dict) -> str:
    """Flatten weave payload into a compact, prompt-ready context block."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    sections: list[str] = []

    ctx_tier2 = weave.get("tier2_context", "")
    ctx_tier1 = weave.get("tier1_context", "")
    nudge = weave.get("memory_nudge", "")
    prompt_block = weave.get("prompt_block", "")

    if prompt_block:
        sections.append(prompt_block)
    else:
        if ctx_tier2:
            sections.append(f"[深层记忆]\n{ctx_tier2}")
        if ctx_tier1 and not ctx_tier2:
            sections.append(f"[场景感知]\n{ctx_tier1}")
        if nudge:
            sections.append(f"[重点提醒]\n{nudge}")

    if not sections:
        return ""

    body = "\n\n".join(sections)
    header = (
        "[memory-protocol] 已加载跨会话长期记忆（时间 " + now + "）。"
        "先依据此记忆作答；如需更深入检索可调用 memory_search。"
    )
    return _truncate(f"{header}\n\n{body}", MAX_CTX_CHARS)


_CLASSIFY_GUIDE = (
    "[memory-protocol] 记忆系统要求每个新轮次先完成协议动作。\n"
    "请依次调用（均为 MCP 工具）：\n"
    "1. memory_classify(category=chat|skill|mission|pref|pers) — 对上一轮对话分类\n"
    "2. memory_weave(user_message=本轮用户消息) — 加载长期记忆上下文\n"
    "完成后再执行用户任务。这两步会解除后续工具调用的协议限制。"
)


def main() -> None:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except ValueError:
        return

    session_id = event.get("session_id") or "unknown"
    prompt = event.get("prompt", "")

    state.reset(session_id)

    if not daemon_client.ensure_daemon():
        return
    result = daemon_client.weave(user_message=prompt)
    if not result:
        return

    if "error" in result:
        err = result["error"]
        if "ClassificationRequired" in err:
            payload = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _CLASSIFY_GUIDE,
                }
            }
            json.dump(payload, sys.stdout)
            sys.stdout.write("\n")
        return

    context = _render_context(result)
    if not context:
        return

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
