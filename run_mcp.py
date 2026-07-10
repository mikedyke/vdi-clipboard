#!/usr/bin/env python
"""Entry point for the local-driver MCP server (register this with Claude Code)."""
import os
import sys

# See run_helper.py for why this is needed on some Python distributions.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vdi_channel.mcp_server import main

if __name__ == "__main__":
    sys.exit(main())
