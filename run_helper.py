#!/usr/bin/env python
"""Entry point for the in-session VDI helper (run this INSIDE the remote session)."""
import os
import sys

# Some Python distributions (e.g. embeddable/portable builds pinned via a
# python3XX._pth file) don't add the running script's own directory to
# sys.path. Do it explicitly so `vdi_channel` resolves regardless of interpreter.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vdi_channel.helper import main

if __name__ == "__main__":
    sys.exit(main())
