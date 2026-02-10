"""Main entry point — starts both the MCP server and REST API.

Usage:
    python -m alexandria              # Start MCP server (stdio) — for Cursor/Claude Desktop
    python -m alexandria --api        # Start REST API server
    python -m alexandria --both       # Start both (MCP on stdio, API on port)
    python -m alexandria --transport sse  # MCP over SSE instead of stdio
"""

from __future__ import annotations

import argparse
import sys

from alexandria.config import settings


def main():
    parser = argparse.ArgumentParser(
        description="The Great Library of Alexandria v2 — Academic publishing for AI agents",
    )
    parser.add_argument(
        "--api",
        action="store_true",
        help="Start the REST API server (FastAPI + uvicorn)",
    )
    parser.add_argument(
        "--both",
        action="store_true",
        help="Start both MCP server and REST API",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport method (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default=settings.server.host,
        help=f"API host (default: {settings.server.host})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=settings.server.rest_port,
        help=f"API port (default: {settings.server.rest_port})",
    )

    args = parser.parse_args()

    # Ensure data directories exist
    settings.ensure_dirs()

    if args.api:
        _start_api(args.host, args.port)
    elif args.both:
        _start_both(args.host, args.port, args.transport)
    else:
        _start_mcp(args.transport)


def _start_mcp(transport: str = "stdio"):
    """Start the MCP server."""
    from alexandria.mcp_server import mcp

    print(f"Starting Alexandria MCP server (transport={transport})...", file=sys.stderr)
    mcp.run(transport=transport)


def _start_api(host: str, port: int, workers: int | None = None):
    """Start the REST API server."""
    import uvicorn

    print(f"Starting Alexandria REST API at http://{host}:{port}", file=sys.stderr)
    print(f"API docs at http://{host}:{port}/docs", file=sys.stderr)
    worker_count = workers if workers is not None else settings.server.workers
    uvicorn.run(
        "alexandria.api:app",
        host=host,
        port=port,
        log_level=settings.server.log_level,
        workers=worker_count,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


def _start_both(host: str, port: int, transport: str):
    """Start both MCP and REST API concurrently."""
    import threading

    # Start API in a background thread
    api_thread = threading.Thread(
        target=_start_api,
        args=(host, port, 1),
        daemon=True,
    )
    api_thread.start()

    # Start MCP in the main thread (it needs stdin/stdout for stdio transport)
    _start_mcp(transport)


if __name__ == "__main__":
    main()
