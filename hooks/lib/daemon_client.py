"""HTTP client for the memory daemon — used by Claude Code hooks.

All calls fail open: if the daemon is down or times out, hooks must never
block the agent. Callers decide what to do with a None / error return.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request

DAEMON_HOST = os.environ.get("CCMP_DAEMON_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("CCMP_DAEMON_PORT", "8000"))
DAEMON_BASE = f"http://{DAEMON_HOST}:{DAEMON_PORT}"

HOOK_TIMEOUT_S = float(os.environ.get("CCMP_HOOK_TIMEOUT_S", "2.5"))
START_TIMEOUT_S = float(os.environ.get("CCMP_START_TIMEOUT_S", "8.0"))

_DAEMON_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "daemon",
    "cli.py",
)
_BOOTSTRAP_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "bin",
    "ccmp-bootstrap",
)
# Python interpreter that can import daemon deps (memory_skill/starlette/mcp).
# Hooks run under the system python3, which lacks them. CCMP_PYTHON overrides;
# otherwise bootstrap's venv (plugin_root/.venv) is used if present.
DAEMON_PYTHON = os.environ.get("CCMP_PYTHON") or sys.executable


def _resolve_daemon_python() -> str:
    if os.environ.get("CCMP_PYTHON"):
        return os.environ["CCMP_PYTHON"]
    rel = ["..", "..", ".venv", "Scripts" if os.name == "nt" else "bin", "python"]
    venv_py = os.path.join(os.path.dirname(_DAEMON_PY), *rel)
    venv_py = os.path.normpath(venv_py)
    if os.path.exists(venv_py):
        return venv_py
    return sys.executable


def _post(path: str, payload: dict, timeout: float) -> dict | None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DAEMON_BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, socket.timeout, OSError, ValueError):
        return None


def _get(path: str, timeout: float) -> dict | None:
    try:
        with urllib.request.urlopen(DAEMON_BASE + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, socket.timeout, OSError, ValueError):
        return None


def health() -> bool:
    return _get("/health", timeout=1.0) is not None


def ensure_daemon() -> bool:
    """Return True if the daemon is reachable; bootstrap + start it if not."""
    if health():
        return True
    try:
        subprocess.run(
            [sys.executable, _BOOTSTRAP_PY],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=START_TIMEOUT_S * 10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [_resolve_daemon_python(), _DAEMON_PY, "--transport", "http", "--port", str(DAEMON_PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except OSError:
        return False
    for _ in range(int(START_TIMEOUT_S * 2)):
        import time

        time.sleep(0.5)
        if health():
            return True
    return False


def weave(user_message: str = "", assistant_content: str = "") -> dict | None:
    payload = {"user_message": user_message, "assistant_content": assistant_content}
    return _post("/weave", payload, timeout=HOOK_TIMEOUT_S)


def ingest(content: str, role: str = "user") -> dict | None:
    return _post("/ingest", {"content": content, "role": role}, timeout=HOOK_TIMEOUT_S)


def classify(category: str = "chat", note: str = "") -> dict | None:
    return _post("/classify", {"category": category, "note": note}, timeout=HOOK_TIMEOUT_S)
