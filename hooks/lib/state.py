"""Weave-gate state shared by gate.py and weave_inject.py.

A fresh user prompt (UserPromptSubmit) opens a new turn: the gate resets to
pending. Calling memory_weave marks the turn as consulted, allowing non-memory
tools through until the next prompt.
"""

from __future__ import annotations

import json
import os
import time

STATE_DIR = os.environ.get("CCMP_STATE_DIR", os.path.join(os.path.expanduser("~"), ".claude", "state"))
_TURN_TTL_S = 3600


def _path(session_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return os.path.join(STATE_DIR, f"{safe}.gate")


def reset(session_id: str) -> None:
    try:
        os.remove(_path(session_id))
    except OSError:
        pass


def mark_weaved(session_id: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_path(session_id), "w", encoding="utf-8") as f:
        json.dump({"weaved": True, "ts": time.time()}, f)


def is_weaved(session_id: str) -> bool:
    try:
        with open(_path(session_id), encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("weaved")) and data.get("ts", 0) > time.time() - _TURN_TTL_S
    except (OSError, ValueError):
        return False
