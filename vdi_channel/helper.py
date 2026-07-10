"""In-session helper REPL (§6) — the responder half of the CBP state machine.

Loop: poll for a REQ with a new nonce, decode it, run ``dispatch()`` (a generator
of logical messages), and stream each back as RSP frames — compressing, chunking
under the probed clipboard cap, and stop-and-wait ACKing per §3.3. On the final
message it awaits FIN; on a command failure it emits ERR. After the exchange it
writes IDLE to scrub the slot (§3.3).
"""

from __future__ import annotations

import logging
import sys
import time

from . import codec, commands
from .config import Config
from .errors import ChannelError, CommandError, TransportTimeout
from .transport import ClipboardTransport

log = logging.getLogger("vdi.helper")
_SENTINEL = object()


class Helper:
    def __init__(self, transport: ClipboardTransport | None = None, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self.t = transport or ClipboardTransport(self.cfg)
        self._processed: set[str] = set()
        self.cap = self.t.cap()   # probe once at startup (§3.6)
        if self.cfg.scrub_on_start:
            # Clear any stale REQ/RSP left by a prior run so we never commit to a
            # dead exchange (blocking on an ACK/FIN from a requester that's gone).
            self.t.scrub()

    # -- main loop ---------------------------------------------------------- #
    def serve_forever(self) -> None:
        log.info("helper ready: backend=%s cap=%d compress=%s",
                 self.t.cb.name, self.cap, self.cfg.compress)
        while True:
            try:
                self._serve_once()
            except KeyboardInterrupt:  # pragma: no cover
                log.info("helper stopping")
                return
            except ChannelError as e:
                log.warning("exchange aborted: %s", e)

    def serve_once(self, timeout_ms: int = 24 * 3600 * 1000) -> bool:
        """One-shot mode (§6 override): handle a single REQ then return."""
        return self._serve_once(timeout_ms)

    def _serve_once(self, timeout_ms: int = 24 * 3600 * 1000) -> bool:
        frame = self.t.read_frame(timeout_ms)
        if frame is None or frame.type != codec.REQ:
            return False
        if frame.nonce in self._processed:
            return False
        self._processed.add(frame.nonce)

        nonce = frame.nonce
        request = codec.decode_payload(frame.enc, frame.comp, frame.payload).decode(
            "utf-8", "replace"
        )
        log.info("command %s: %s", nonce, request)  # timestamped by the log format

        started = time.monotonic()
        outcome = "ok"
        try:
            gen = commands.dispatch(request, self.cfg, self.cap)
            self._stream(nonce, gen)
        except CommandError as e:
            outcome = f"error({e.code})"
            self._send_error(nonce, e)
        except ChannelError as e:
            outcome = f"transport_error({e.code})"
            raise
        except Exception as e:  # unexpected command bug -> ERR, not a crash
            outcome = f"error({type(e).__name__})"
            self._send_error(nonce, CommandError(f"{type(e).__name__}: {e}"))
        finally:
            log.info("command %s done in %dms: %s",
                     nonce, int((time.monotonic() - started) * 1000), outcome)
            self._finalize(nonce)
        return True

    # -- response streaming ------------------------------------------------- #
    def _stream(self, nonce: str, gen) -> None:
        """Emit each yielded message; the last one carries END, the rest MORE."""
        it = iter(gen)
        prev = next(it, _SENTINEL)
        if prev is _SENTINEL:
            self._send_message(nonce, 1, b"", is_final=True)  # always terminate
            return
        idx = 0
        while prev is not _SENTINEL:
            cur = next(it, _SENTINEL)
            is_final = cur is _SENTINEL
            idx += 1
            self._send_message(nonce, idx, prev, is_final=is_final)
            prev = cur

    def _send_message(self, nonce: str, msg: int, data: bytes, is_final: bool) -> None:
        enc, comp, wire = codec.encode_payload(
            data, self.cfg.compress, self.cfg.compress_min_bytes
        )
        chunk_max = self.cfg.chunk_max(self.cap)
        chunks = codec.split_chunks(wire, chunk_max)
        total = len(chunks)
        for i, chunk in enumerate(chunks, start=1):
            flags = set()
            if i == total:
                flags.add(codec.END if is_final else codec.MORE)
            frame = codec.Frame(
                codec.RSP, nonce, msg=msg, seq=i, total=total,
                enc=enc, comp=comp, payload=chunk, flags=flags,
            )
            self.t.write_frame(frame)
            # The exchange-final END frame is acked by FIN; everything else by ACK.
            if frame.has(codec.END):
                self._await(nonce, want=codec.FIN)
            else:
                self._await(nonce, want=codec.ACK, msg=msg, seq=i)

    def _send_error(self, nonce: str, err: CommandError) -> None:
        payload = f"{err.code} {err.message}".encode("utf-8")
        enc, comp, wire = codec.encode_payload(payload, "none", 1 << 30)  # keep it plain
        frame = codec.Frame(codec.ERR, nonce, msg=1, seq=1, total=1,
                            enc=enc, comp=comp, payload=wire, flags={codec.END})
        self.t.write_frame(frame)
        self._await(nonce, want=codec.FIN)

    # -- flow control ------------------------------------------------------- #
    def _await(self, nonce: str, want: str, msg: int = 0, seq: int = 0,
               timeout_s: float = 30.0) -> None:
        """Poll until the requester's ACK/FIN for this frame arrives (§3.3)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            remaining_ms = int(max(0, deadline - time.monotonic()) * 1000)
            frame = self.t.read_frame(min(4 * self.cfg.poll_interval_ms, remaining_ms) or 1)
            if frame is None:
                # No reply yet — if a user copy clobbered our frame, put it back.
                self.t.reassert()
                continue
            if frame.nonce != nonce:
                continue
            if frame.type != want:
                continue
            if want == codec.ACK and (frame.msg != msg or frame.seq != seq):
                continue
            return
        raise TransportTimeout(f"no {want} for nonce {nonce} msg={msg} seq={seq}")

    def _finalize(self, nonce: str) -> None:
        """After FIN, scrub the slot to IDLE so a stale RSP can't be re-read."""
        self.t.write_frame(codec.make_idle(nonce))
        log.info("exchange %s done", nonce)


def main(argv=None) -> int:
    import argparse

    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="VDI in-session clipboard helper (CBP responder)")
    p.add_argument("--oneshot", action="store_true", help="handle a single request then exit")
    p.add_argument("--no-probe", action="store_true", help="skip empirical cap discovery")
    args = p.parse_args(argv)

    cfg = Config.from_env()
    if args.oneshot:
        cfg.mode = "oneshot"
    if args.no_probe:
        cfg.probe_cap_on_start = False

    helper = Helper(cfg=cfg)
    if cfg.mode == "oneshot":
        helper.serve_once()
    else:
        helper.serve_forever()
    return 0
