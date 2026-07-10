#!/usr/bin/env python
"""Local CLI for driving the channel without MCP (useful for testing).

Usage (with a helper running, possibly in another session sharing the clipboard):
    python vdi_cli.py ping
    python vdi_cli.py exec "Get-Process | Select-Object -First 5"
    python vdi_cli.py grep C:/logs/app.log ERROR 2 100
"""
import os
import sys

# See run_helper.py for why this is needed on some Python distributions.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vdi_channel.channel import LocalChannel
from vdi_channel.config import Config


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    line = " ".join(_q(a) for a in argv)
    channel = LocalChannel(cfg=Config.from_env())
    print(channel.request(line))
    return 0


def _q(a: str) -> str:
    if a and not any(c.isspace() for c in a) and "'" not in a:
        return a
    return "'" + a.replace("'", "''") + "'"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
