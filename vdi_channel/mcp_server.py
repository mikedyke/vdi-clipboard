"""MCP server (local driver) — exposes the CBP channel as Claude Code tools.

Each tool builds a request line, runs one full clipboard exchange via
``LocalChannel.request`` (send_query + read_responses, §9), and returns the
reassembled text. The channel serializes exchanges internally (one shared slot),
so concurrent tool calls are safe. Logs go to stderr — stdout is the MCP stdio
transport and must stay clean.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from .channel import LocalChannel
from .config import Config
from .errors import ChannelError

logging.basicConfig(
    level=logging.INFO, stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("vdi.mcp")

cfg = Config.from_env()
channel = LocalChannel(cfg=cfg)
mcp = FastMCP("vdi-clipboard")


def _run(line: str) -> str:
    try:
        return channel.request(line)
    except ChannelError as e:
        return f"ERROR {e.code}: {e.message}"


@mcp.tool()
def vdi_ping() -> str:
    """Check the in-session helper is alive. Returns its version and clipboard cap."""
    return _run("ping")


@mcp.tool()
def vdi_exec(command: str) -> str:
    """Run a PowerShell command inside the remote VDI session and return its output.

    The command executes in-session; only the result crosses the clipboard channel
    (compressed + chunked as needed). Prefer vdi_grep/vdi_read for large files.
    """
    return _run(f"exec {command}")


@mcp.tool()
def vdi_read(path: str, offset: int = 0, limit: int = 65536) -> str:
    """Read a byte window of a file in the remote session ([offset, offset+limit))."""
    return _run(f"read {_arg(path)} {offset} {limit}")


@mcp.tool()
def vdi_grep(path: str, pattern: str, context: int = 0, max_matches: int = 500) -> str:
    """Search a remote file for a pattern (in-session Select-String); return matches only."""
    return _run(f"grep {_arg(path)} {_arg(pattern)} {context} {max_matches}")


@mcp.tool()
def vdi_stat(path: str) -> str:
    """Return size, mtime and type for a path in the remote session."""
    return _run(f"stat {_arg(path)}")


@mcp.tool()
def vdi_ls(path: str = ".", glob: str = "*") -> str:
    """List a directory in the remote session, optionally filtered by a glob."""
    return _run(f"ls {_arg(path)} {_arg(glob)}")


@mcp.tool()
def vdi_get(path: str) -> str:
    """Fetch a whole remote file (compressed+chunked). Bounded by max_get_bytes."""
    return _run(f"get {_arg(path)}")


def _arg(s: str) -> str:
    """Quote an argument so it survives the space-delimited request line."""
    if s and not any(c.isspace() for c in s) and "'" not in s and '"' not in s:
        return s
    return "'" + s.replace("'", "''") + "'"


def main() -> int:
    log.info("vdi-clipboard MCP server starting (transport=%s)", cfg.kind)
    mcp.run()
    return 0
