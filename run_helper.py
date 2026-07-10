#!/usr/bin/env python
"""Entry point for the in-session VDI helper (run this INSIDE the remote session)."""
import sys

from vdi_channel.helper import main

if __name__ == "__main__":
    sys.exit(main())
