"""End-to-end protocol tests over an in-process clipboard (no OS clipboard needed).

A shared InMemoryClipboard models the single slot; the helper runs on a thread and
the channel drives it — exercising REQ/RSP, ACK, multi-message MORE/END, chunking,
compression, the ERR path, and coexistence with foreign clipboard writes.
"""
import contextlib
import os
import tempfile
import threading
import time

import pytest

from vdi_channel.channel import LocalChannel
from vdi_channel.clipboard import InMemoryClipboard
from vdi_channel.config import Config
from vdi_channel.errors import CommandError
from vdi_channel.helper import Helper
from vdi_channel.transport import ClipboardTransport


def _cfg(**kw):
    base = dict(probe_cap_on_start=False, default_cap=1200, compress="zstd",
                compress_min_bytes=32, poll_interval_ms=10)
    base.update(kw)
    return Config(**base)


@contextlib.contextmanager
def channel_pair(**cfg_kw):
    """Yield (channel, shared_clipboard) with a helper thread serving in the background."""
    shared = InMemoryClipboard()
    hcfg, lcfg = _cfg(**cfg_kw), _cfg(**cfg_kw)
    helper = Helper(transport=ClipboardTransport(hcfg, clipboard=shared), cfg=hcfg)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                helper.serve_once(timeout_ms=100)
            except Exception:
                pass

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    channel = LocalChannel(transport=ClipboardTransport(lcfg, clipboard=shared), cfg=lcfg)
    try:
        yield channel, shared
    finally:
        stop.set()
        th.join(timeout=2)


def test_ping():
    with channel_pair() as (ch, _):
        out = ch.request("ping", timeout=10)
    assert "pong" in out and "vdi-helper" in out


def test_exec_is_multi_message():
    with channel_pair() as (ch, _):
        out = ch.request("exec echo hello-vdi", timeout=10)
    assert "[exec]" in out          # progress message (MSG=1, MORE)
    assert "hello-vdi" in out       # final output (MSG=2, END)


def test_large_read_chunks_and_compresses():
    # A file bigger than the tiny cap forces multi-chunk framing + reassembly.
    payload = ("The quick brown fox. " * 500).encode("utf-8")  # ~10 KB, compressible
    with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as tf:
        tf.write(payload)
        path = tf.name
    try:
        with channel_pair() as (ch, _):
            out = ch.request(f"read '{path}' 0 100000", timeout=15)
        assert out.encode("utf-8") == payload
    finally:
        os.unlink(path)


def test_unknown_command_raises_command_error():
    with channel_pair() as (ch, _):
        with pytest.raises(CommandError):
            ch.request("nope_not_a_command", timeout=10)


def test_survives_foreign_clipboard_writes():
    # Simulate a user copying text during the exchange; it must be ignored and the
    # clobbered frame retransmitted, so the result still reassembles.
    payload = ("data line. " * 400).encode("utf-8")
    with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as tf:
        tf.write(payload)
        path = tf.name
    try:
        with channel_pair() as (ch, shared):
            stop = threading.Event()

            def interfere():
                i = 0
                while not stop.is_set():
                    i += 1
                    shared.set_text(f"USER COPIED {i}")
                    time.sleep(0.03)

            t = threading.Thread(target=interfere, daemon=True)
            t.start()
            out = ch.request(f"read '{path}' 0 100000", timeout=30)
            stop.set()
            t.join(timeout=2)
        assert out.encode("utf-8") == payload
    finally:
        os.unlink(path)


def test_restore_user_clipboard():
    with channel_pair(restore_user_clipboard=True) as (ch, shared):
        shared.set_text("IMPORTANT USER DATA")
        ch.request("ping", timeout=10)
        assert shared.get_text() == "IMPORTANT USER DATA"
