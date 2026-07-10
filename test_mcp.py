"""Full-stack test: real MCP server subprocess (stdio) + helper thread + clipboard.

This is the exact topology Claude Code uses: an MCP client speaks the protocol to
run_mcp.py over stdio; that server drives the clipboard channel to the in-session
helper (here a thread in this process, sharing the OS clipboard).
"""
import asyncio
import os
import sys
import threading
import time

import pyperclip

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from vdi_channel.config import Config
from vdi_channel.helper import Helper

ENV = {
    "VDI_PROBE_CAP_ON_START": "false",
    "VDI_DEFAULT_CAP": "1200",
    "VDI_COMPRESS": "zstd",
    "VDI_COMPRESS_MIN_BYTES": "32",
    "VDI_POLL_INTERVAL_MS": "40",
}


def start_helper(stop):
    cfg = Config(probe_cap_on_start=False, default_cap=1200, compress="zstd",
                 compress_min_bytes=32, poll_interval_ms=40)
    helper = Helper(cfg=cfg)
    while not stop.is_set():
        try:
            helper.serve_once(timeout_ms=400)
        except Exception as e:
            print("helper:", e, file=sys.stderr)


async def main():
    pyperclip.copy("")
    stop = threading.Event()
    threading.Thread(target=start_helper, args=(stop,), daemon=True).start()
    time.sleep(0.3)

    env = dict(os.environ)
    env.update(ENV)
    params = StdioServerParameters(command=sys.executable, args=["run_mcp.py"], env=env)

    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            for name, args in [
                ("vdi_ping", {}),
                ("vdi_exec", {"command": "1..50 | ForEach-Object { \"row $_\" }"}),
                ("vdi_stat", {"path": "C:/Projects/vdi-clipboard/requirements.txt"}),
            ]:
                res = await session.call_tool(name, args)
                text = res.content[0].text if res.content else ""
                head = text.replace("\n", " ")[:120]
                print(f"\n[{name}] -> {head}")

    stop.set()
    print("\nMCP integration OK")


if __name__ == "__main__":
    asyncio.run(main())
