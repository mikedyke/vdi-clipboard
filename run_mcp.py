#!/usr/bin/env python
"""Entry point for the local-driver MCP server (register this with Claude Code)."""
import sys

from vdi_channel.mcp_server import main

if __name__ == "__main__":
    sys.exit(main())
