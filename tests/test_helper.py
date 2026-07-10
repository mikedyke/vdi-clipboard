"""Unit tests for Helper._finalize's stale-IDLE-scrub guard.

Reproduces the failure mode surfaced by a duplicate-helper collision: an
exchange that stalls (e.g. its ACK never arrives because another helper already
answered and closed it) must not clobber a newer REQ the requester has since
started while finalizing the old, abandoned exchange.
"""
from vdi_channel import codec
from vdi_channel.clipboard import InMemoryClipboard
from vdi_channel.config import Config
from vdi_channel.helper import Helper
from vdi_channel.transport import ClipboardTransport


def _make_helper():
    cfg = Config(probe_cap_on_start=False, default_cap=1200)
    shared = InMemoryClipboard()
    helper = Helper(transport=ClipboardTransport(cfg, clipboard=shared), cfg=cfg)
    return helper, shared


def test_finalize_skips_scrub_when_newer_req_present():
    helper, shared = _make_helper()
    newer_req = codec.Frame(codec.REQ, "newnonce1", msg=0, seq=1, total=1,
                            enc="A", comp="-", payload=b"ping")
    shared.set_text(newer_req.to_text())

    helper._finalize("stalenonce")  # the old, abandoned exchange finalizing late

    # The newer REQ must survive untouched -- not overwritten with IDLE.
    remaining = codec.parse(codec.normalize(shared.get_text()))
    assert remaining.type == codec.REQ
    assert remaining.nonce == "newnonce1"


def test_finalize_scrubs_normally_when_slot_is_idle_or_empty():
    helper, shared = _make_helper()
    shared.set_text("")  # nothing pending

    helper._finalize("somenonce")

    written = codec.parse(codec.normalize(shared.get_text()))
    assert written.type == codec.IDLE


def test_finalize_scrubs_when_slot_holds_own_stale_frame():
    helper, shared = _make_helper()
    # Slot holds an RSP/ACK/FIN of the SAME exchange (not a different REQ) -- still safe to scrub.
    shared.set_text(codec.make_fin("samenonce").to_text())

    helper._finalize("samenonce")

    written = codec.parse(codec.normalize(shared.get_text()))
    assert written.type == codec.IDLE
