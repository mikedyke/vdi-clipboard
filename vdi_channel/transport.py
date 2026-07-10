"""Clipboard transport (§4, §5).

Physical I/O plus the truncation/settle retry appropriate to the clipboard.
Header parse/serialize, base64, compression, CRC and chunk split/join live in
``codec`` and are shared. ``channel`` never branches on transport type.

Dedup strategy for the three races (§3.4): each side records ``_last_raw`` — the
normalized text it last wrote OR last returned from a read. ``read_frame`` only
returns a value that differs from ``_last_raw``, which simultaneously prevents
(1) re-reading our own writes, and (2) duplicate reads of a peer frame. The
LEN/CRC check inside ``read_frame`` covers (3) partial-sync truncation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import codec
from .clipboard import Clipboard
from .config import Config
from .errors import CrcMismatch, LenMismatch, TruncatedRetryExhausted


@dataclass
class TransportCaps:
    max_payload: int
    direction: str = "bidirectional"
    lossy: bool = False
    needs_ack: bool = True


class ClipboardTransport:
    def __init__(self, cfg: Config | None = None, clipboard: Clipboard | None = None):
        self.cfg = cfg or Config()
        self.cb = clipboard or Clipboard()
        self.poll = self.cfg.poll_interval_ms / 1000.0
        self._last_raw: str | None = None
        self._last_written: str | None = None
        self._cap: int | None = None

    # -- physical write ----------------------------------------------------- #
    def write_frame(self, frame: codec.Frame) -> None:
        text = frame.to_text()
        self.cb.set_text(text)
        self._last_raw = codec.normalize(text)
        self._last_written = text

    def reassert(self) -> bool:
        """Re-post our last written frame iff a *foreign* value clobbered the slot.

        Used by whichever side is waiting to have its last frame acknowledged: if
        real-user/other-app clipboard activity overwrote an in-flight frame, put it
        back. A CBP frame on the slot (our own, or the peer's reply) is never
        overwritten — only genuine non-CBP interference triggers a retransmit.
        """
        if self._last_written is None:
            return False
        cur = self.cb.get_text()
        if cur is not None and codec.normalize(cur).startswith(codec.MAGIC):
            return False  # slot holds a CBP frame — leave the protocol alone
        self.cb.set_text(self._last_written)
        self._last_raw = codec.normalize(self._last_written)
        return True

    def scrub(self) -> None:
        """Render IDLE so a stale RSP can never be re-read as fresh (§3.3)."""
        self.write_frame(codec.make_idle())

    # -- physical read ------------------------------------------------------ #
    def read_frame(self, timeout_ms: int) -> codec.Frame | None:
        """Return the next new, valid frame, or None on timeout.

        Foreign (non-CBP) clipboard values are ignored. LEN/CRC failures trigger
        the partial-sync retry (§3.4 #2); after ``truncation_retries`` consecutive
        bad reads of the *same* changing value, raise TruncatedRetryExhausted.
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        bad = 0
        while True:
            raw = self.cb.get_text()
            if raw is not None:
                norm = codec.normalize(raw)
                if norm and norm != self._last_raw and norm.startswith(codec.MAGIC):
                    try:
                        frame = codec.parse(norm)
                    except (LenMismatch, CrcMismatch):
                        # Value may still be settling; re-poll and let it stabilize.
                        bad += 1
                        if bad >= self.cfg.truncation_retries:
                            raise TruncatedRetryExhausted(
                                f"clipboard unstable after {bad} reads"
                            )
                        time.sleep(self.poll)
                        continue
                    self._last_raw = norm
                    return frame
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.poll)

    # -- capabilities ------------------------------------------------------- #
    def probe(self) -> TransportCaps:
        cap = self._cap if self._cap is not None else self._discover_cap()
        return TransportCaps(max_payload=cap, direction="bidirectional", lossy=False, needs_ack=True)

    def cap(self) -> int:
        if self._cap is None:
            self._discover_cap()
        return self._cap  # type: ignore[return-value]

    def _discover_cap(self) -> int:
        """Empirically find the clipboard payload ceiling (§3.6) by binary search.

        Writes a self-test payload, reads it back, compares. Restores nothing —
        callers should probe before an exchange begins. Falls back to the
        configured default if probing is disabled or the clipboard is flaky.
        """
        if not self.cfg.probe_cap_on_start:
            self._cap = self.cfg.default_cap
            return self._cap

        def round_trips(n: int) -> bool:
            token = ("cap-probe-" + "x" * max(0, n - 10))[:n]
            try:
                self.cb.set_text(token)
                back = self.cb.get_text()
            except Exception:
                return False
            return back == token

        lo, hi = 256, self.cfg.probe_cap_max
        if round_trips(hi):
            good = hi
        else:
            good = lo if round_trips(lo) else 0
            while hi - lo > 1024:
                mid = (lo + hi) // 2
                if round_trips(mid):
                    good, lo = mid, mid
                else:
                    hi = mid
        self._cap = good or self.cfg.default_cap
        self._last_raw = None  # probe scribbled on the slot; forget it
        return self._cap
