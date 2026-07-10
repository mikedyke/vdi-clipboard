"""VDI data-channel — CBP (Clipboard Protocol) implementation.

Implements the request/response data channel described in ``data-channel-spec.md``
over the clipboard transport (§3, §5). Two halves share ``codec`` + ``transport``:

* ``LocalChannel`` (channel.py) — the requester half, wrapped by the MCP server.
* ``Helper``       (helper.py)  — the in-session responder that runs PowerShell.
"""

__all__ = ["codec", "transport", "channel", "helper", "commands", "config", "errors"]
__version__ = "1.0"
