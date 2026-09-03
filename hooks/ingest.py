#!/usr/bin/env python3
"""Stop hook — auto-ingest the finished turn into memory.

Mirrors dsh `agent/turn-stopping`. Claude Code hook input carries
`session_id`, `transcript_path`, and `last_assistant_message`. The transcript
may lag the in-memory conversation at Stop time, so we use
`last_assistant_message` for the assistant side and read the transcript only
for the user prompt of this turn (offset-tracked, so each user message is
ingested exactly once).

Ingests pairs into the shared daemon; any failure fails open (turn already
finished, nothing to block).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from lib import daemon_client  # noqa: E402

OFFSET_DIR = os.environ.get("CCMP_STATE_DIR", os.path.join(os.path.expanduser("~"), ".claude", "state"))
MAX_INGEST_CHARS = int(os.environ.get("CCMP_MAX_INGEST_CHARS", "3000"))


def _offset_path(session_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return os.path.join(OFFSET_DIR, f"{safe}.offset")


def _read_offset(session_id: str) -> int:
    try:
        with open(_offset_path(session_id), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _write_offset(session_id: str, line_no: int) -> None:
    os.makedirs(OFFSET_DIR, exist_ok=True)
    with open(_offset_path(session_id), "w", encoding="utf-8") as f:
        f.write(str(line_no))


def _user_texts_from_transcript(transcript_path: str, start_line: int) -> list[str]:
    """Collect user prompt texts (type=user, message.content str) after offset.

    Tool-call results and system noise appear under other types; only
    real user turns carry a plain-string content with type=user.
    """
    texts: list[str] = []
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                if line_no <= start_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "user":
                    continue
                if d.get("isSidechain"):
                    continue
                msg = d.get("message", {})
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    texts.append(content.strip())
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") in ("text", "input_text"):
                            t = block.get("text", "")
                            if t.strip():
                                texts.append(t.strip())
    except OSError:
        return texts
    return texts


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2):]
    return f"{head}\n…[truncated {len(text) - limit} chars]…\n{tail}"


def main() -> None:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except ValueError:
        return

    session_id = event.get("session_id") or "unknown"
    transcript_path = event.get("transcript_path") or ""
    last_assistant = event.get("last_assistant_message") or ""
    stop_hook_active = event.get("stop_hook_active") is True

    if stop_hook_active:
        return  # continuation from a previous stop-hook block: skip re-ingest

    if not daemon_client.ensure_daemon():
        return

    offset = _read_offset(session_id)
    user_texts = _user_texts_from_transcript(transcript_path, offset) if transcript_path else []

    for text in user_texts:
        daemon_client.ingest(_clip(text, MAX_INGEST_CHARS), role="user")

    if last_assistant.strip():
        daemon_client.ingest(_clip(last_assistant.strip(), MAX_INGEST_CHARS), role="assistant")

    # Advance offset past every line we could have seen (safe upper bound:
    # the transcript file length at read time).
    if transcript_path:
        try:
            with open(transcript_path, encoding="utf-8") as f:
                total = sum(1 for _ in f)
            if total > offset:
                _write_offset(session_id, total)
        except OSError:
            pass

    _signal_backup()


def _signal_backup() -> None:
    """Arm the ccmp-backup daemon (debounced) so an idle session backs up.

    Cloned-instance signal file is shared across agents on this machine:
    ccmp-backup daemon watches ~/.ccmp-backup/signal. SOLO_MEMORY_BACKUP_SIGNAL
    is honoured for consistency with the opencode plugin; '0' disables.
    """
    env_val = os.environ.get("SOLO_MEMORY_BACKUP_SIGNAL")
    if env_val == "0":
        return
    signal_path = env_val or os.path.join(os.path.expanduser("~"), ".ccmp-backup", "signal")
    try:
        with open(signal_path, "a", encoding="utf-8") as f:
            f.write(datetime.now(UTC).isoformat() + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
