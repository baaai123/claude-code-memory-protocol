"""daemon CLI — 选择 stdio(子进程) 或 http(常驻 daemon)。"""

from __future__ import annotations

import argparse
import os
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ccmp", description="claude-code-memory-protocol daemon")
    p.add_argument("--transport", choices=["stdio", "http"], default="http")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    return p


def main() -> None:
    args = build_parser().parse_args()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    if args.transport == "stdio":
        from memory_skill.mcp_server import main as stdio_main

        import asyncio

        asyncio.run(stdio_main())
    else:
        import uvicorn

        from daemon.server import app

        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
