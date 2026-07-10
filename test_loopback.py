"""End-to-end loopback test: helper thread + local channel over the real clipboard.

Both sides share this machine's OS clipboard (one slot, two Transport instances)
— the same topology as local-driver <-> in-session helper, minus the VDI boundary.
A deliberately tiny cap forces multi-chunk framing so chunking is exercised.
"""
import logging
import sys
import threading
import time

from vdi_channel.channel import LocalChannel
from vdi_channel.config import Config
from vdi_channel.helper import Helper

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def make_cfg():
    # Tiny cap + low compress threshold to exercise compression AND chunking.
    return Config(probe_cap_on_start=False, default_cap=1200,
                  compress="zstd", compress_min_bytes=32, poll_interval_ms=40)


def main():
    stop = threading.Event()
    helper = Helper(cfg=make_cfg())

    def run_helper():
        while not stop.is_set():
            try:
                helper.serve_once(timeout_ms=500)
            except Exception as e:
                logging.warning("helper: %s", e)

    th = threading.Thread(target=run_helper, daemon=True)
    th.start()
    time.sleep(0.3)

    channel = LocalChannel(cfg=make_cfg())
    ok = True

    def check(name, got, must_contain):
        nonlocal ok
        passed = all(s in got for s in must_contain)
        ok = ok and passed
        print(f"\n=== {name}: {'PASS' if passed else 'FAIL'} ===")
        print(got[:600] + ("..." if len(got) > 600 else ""))

    # 1) ping — single message
    check("ping", channel.request("ping"), ["pong", "vdi-helper/1"])

    # 2) exec — multi-message (progress + output), short
    check("exec echo", channel.request("exec Write-Output 'hello from vdi'"),
          ["[exec]", "hello from vdi"])

    # 3) exec producing a large output — forces compression + multi-chunk final
    big = channel.request("exec 1..400 | ForEach-Object { \"line $_\" }")
    check("exec large (chunked+compressed)", big, ["line 1", "line 400"])

    # 4) stat on a known file
    check("stat", channel.request("stat 'C:/Projects/vdi-clipboard/requirements.txt'"),
          ["path=", "size=", "type=file"])

    stop.set()
    time.sleep(0.2)
    print("\n" + ("ALL PASS" if ok else "SOME FAILURES"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
