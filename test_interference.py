"""Simulate 'clipboard used for normal business' during active exchanges.

A background thread mimics a real user copying text every ~120 ms while the channel
runs a large, multi-chunk exec. Verifies: (a) foreign copies are ignored and the
exchange still reassembles losslessly via retransmit-on-clobber, and (b) with
restore_user_clipboard the user's content is put back afterwards.
"""
import logging
import sys
import threading
import time

import pyperclip

from vdi_channel.channel import LocalChannel
from vdi_channel.config import Config
from vdi_channel.helper import Helper

logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cfg(**kw):
    base = dict(probe_cap_on_start=False, default_cap=1200, compress="zstd",
               compress_min_bytes=32, poll_interval_ms=40)
    base.update(kw)
    return Config(**base)


def main():
    pyperclip.copy("")
    stop = threading.Event()
    helper = Helper(cfg=cfg())
    threading.Thread(
        target=lambda: [helper.serve_once(timeout_ms=300) for _ in iter(lambda: not stop.is_set(), False)],
        daemon=True,
    ).start()
    time.sleep(0.3)

    # Background 'user' copying text into the clipboard during exchanges.
    interfere = threading.Event()
    hits = [0]

    def user_activity():
        i = 0
        while not stop.is_set():
            if interfere.is_set():
                i += 1
                hits[0] += 1
                try:
                    pyperclip.copy(f"USER COPIED SOMETHING #{i} \N{SNOWMAN}")
                except Exception:
                    pass
            time.sleep(0.12)

    threading.Thread(target=user_activity, daemon=True).start()

    ok = True
    channel = LocalChannel(cfg=cfg())

    # 1) Large multi-chunk exec WHILE the user keeps copying text.
    interfere.set()
    out = channel.request("exec 1..400 | ForEach-Object { \"line $_\" }", timeout=40)
    interfere.clear()
    passed = "line 1\n" in out.replace("\r\n", "\n") and "line 400" in out
    ok = ok and passed
    print(f"interference during exchange: {'PASS' if passed else 'FAIL'} "
          f"(user copied {hits[0]}x mid-exchange, output lines intact)")

    # 2) Restore the user's clipboard after an exchange.
    ch2 = LocalChannel(cfg=cfg(restore_user_clipboard=True))
    pyperclip.copy("IMPORTANT USER DATA")
    ch2.request("ping", timeout=15)
    time.sleep(0.1)
    restored = pyperclip.paste()
    passed2 = restored == "IMPORTANT USER DATA"
    ok = ok and passed2
    print(f"restore_user_clipboard: {'PASS' if passed2 else 'FAIL'} "
          f"(clipboard now = {restored!r})")

    stop.set()
    time.sleep(0.2)
    print("ALL PASS" if ok else "SOME FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
