"""Run one MCP server over stdio: `python -m autopilot.mcp_servers <name>`.

stdout belongs to the MCP protocol on stdio transport, so structlog is routed
to stderr before the server starts.
"""

from __future__ import annotations

import sys

import structlog


def main() -> int:
    from autopilot.mcp_servers.infra import build_infra_server
    from autopilot.mcp_servers.knowledge import build_knowledge_server
    from autopilot.mcp_servers.telemetry import build_telemetry_server

    builders = {
        "telemetry": build_telemetry_server,
        "infra": build_infra_server,
        "knowledge": build_knowledge_server,
    }
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name not in builders:
        print(f"usage: python -m autopilot.mcp_servers {{{'|'.join(builders)}}}",
              file=sys.stderr)
        return 2

    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))
    builders[name]().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
