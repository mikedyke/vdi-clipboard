"""Local channel API (§8) — the requester half of the CBP state machine (§3.3).

``send_query`` writes a REQ; ``read_responses`` drives stop-and-wait receipt:
verify -> ACK each RSP chunk -> reassemble per message -> decode -> yield. It is
streaming-friendly: message 1 is yielded before message 3 exists. On the final
frame (END) it writes FIN; on ERR it writes FIN and raises CommandError.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from . import codec
from .config import Config
from .errors import CommandError, TransportTimeout
from .transport import ClipboardTransport


@dataclass
class Message:
    index: int          # logical response index (MSG axis, §3.1)
    data: bytes         # reassembled, decompressed original bytes
    is_final: bool      # True when this message carried END

    def text(self, errors: str = "replace") -> str:
        return self.data.decode("utf-8", errors)


class LocalChannel:
    def __init__(self, transport: ClipboardTransport | None = None, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self.t = transport or ClipboardTransport(self.cfg)
        self._lock = threading.Lock()  # one exchange at a time over the single slot

    # -- §8 send_query ------------------------------------------------------ #
    def send_query(self, payload: str) -> str:
        nonce = secrets.token_hex(4)  # 8 hex chars, >= 8 per §3.1
        enc, comp, wire = codec.encode_payload(
            payload.encode("utf-8"), self.cfg.compress, self.cfg.compress_min_bytes
        )
        req = codec.Frame(codec.REQ, nonce, msg=0, seq=1, total=1, enc=enc, comp=comp, payload=wire)
        self._req_text = codec.normalize(req.to_text())
        self.t.write_frame(req)
        return nonce

    # -- §8 read_responses -------------------------------------------------- #
    def read_responses(self, nonce: str, timeout: float | None = None):
        timeout = self.cfg.request_timeout_s if timeout is None else timeout
        deadline = time.monotonic() + timeout
        poll_ms = self.cfg.poll_interval_ms

        chunks: dict[int, dict[int, bytes]] = {}
        meta: dict[int, tuple[str, str]] = {}   # msg -> (enc, comp)
        total_of: dict[int, int] = {}           # msg -> expected chunk count
        final_of: dict[int, bool] = {}          # msg -> terminal chunk had END
        seen: set[tuple[int, int]] = set()       # (msg, seq) dedupe
        done: set[int] = set()
        got_any = False

        while True:
            remaining_ms = int(max(0, deadline - time.monotonic()) * 1000)
            frame = self.t.read_frame(min(poll_ms * 4, remaining_ms) or poll_ms)

            if frame is None:
                if time.monotonic() >= deadline:
                    raise TransportTimeout(f"no response for nonce {nonce}")
                if not got_any:
                    self._reassert_request()
                continue

            if frame.nonce != nonce:
                continue  # stale slot / foreign exchange (§3.4 #1)
            got_any = True

            if frame.type == codec.ERR:
                text = codec.decode_payload(frame.enc, frame.comp, frame.payload).decode(
                    "utf-8", "replace"
                )
                self._close(nonce)
                raise CommandError(text)

            if frame.type != codec.RSP:
                continue  # our own REQ echo, IDLE, etc.

            key = (frame.msg, frame.seq)
            is_terminal = frame.has(codec.END) or frame.has(codec.MORE)

            # ACK every RSP chunk EXCEPT the exchange-final (END) one — FIN acks that,
            # which also avoids two back-to-back local writes over the single slot.
            if not frame.has(codec.END):
                self.t.write_frame(codec.make_ack(nonce, frame.msg, frame.seq, frame.total))

            if key in seen:
                continue  # idempotent: dup already accounted for
            seen.add(key)

            chunks.setdefault(frame.msg, {})[frame.seq] = frame.payload
            meta[frame.msg] = (frame.enc, frame.comp)
            if frame.total > 0:
                total_of[frame.msg] = frame.total
            if is_terminal:
                total_of[frame.msg] = frame.total if frame.total > 0 else frame.seq
                final_of[frame.msg] = frame.has(codec.END)

            m = frame.msg
            if m not in done and m in total_of and m in final_of \
                    and len(chunks[m]) == total_of[m]:
                done.add(m)
                wire = b"".join(chunks[m][s] for s in range(1, total_of[m] + 1))
                enc, comp = meta[m]
                data = codec.decode_payload(enc, comp, wire)
                is_final = final_of[m]
                yield Message(index=m, data=data, is_final=is_final)
                if is_final:
                    self._close(nonce)
                    return

    def _close(self, nonce: str, timeout: float | None = None) -> None:
        """Write FIN and wait for the helper's IDLE scrub before returning.

        Waiting for IDLE confirms the helper consumed our FIN, so the next
        exchange's REQ can't clobber the single slot mid-handshake (§3.3).
        Bounded by ``cfg.close_timeout_s`` — a real cross-VDI clipboard hop can
        take much longer than a same-machine round trip, so if this times out
        silently, the caller has already moved on before the helper's IDLE
        (or even our FIN) actually crossed the boundary.
        """
        self.t.write_frame(codec.make_fin(nonce))
        timeout = self.cfg.close_timeout_s if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.t.read_frame(min(4 * self.cfg.poll_interval_ms,
                                          int((deadline - time.monotonic()) * 1000)) or 1)
            if frame is not None and frame.type == codec.IDLE:
                return
            if frame is None:
                self.t.reassert()  # re-post FIN if a user copy clobbered it

    def _reassert_request(self) -> None:
        """Re-post the REQ only if a *foreign* value clobbered the slot before the
        helper answered. Never overwrite a CBP frame — that could be a fresh RSP
        that read_frame is about to pick up on the next poll."""
        cur = self.t.cb.get_text()
        if cur is not None and codec.normalize(cur).startswith(codec.MAGIC):
            return  # our REQ, or the helper's response — leave it alone
        self.t.cb.set_text(self._req_text)
        self.t._last_raw = self._req_text

    # -- convenience wrapper (§8) ------------------------------------------- #
    def request(self, line: str, timeout: float | None = None) -> str:
        """Run one full exchange; return every message's text joined by newlines.

        Progress messages (emitted before the final) are included in order, so a
        caller sees e.g. an ``[exec] ...`` progress line followed by the output.
        Binary finals are surfaced as a base64 block with a size marker.
        """
        with self._lock:
            saved = self._snapshot_user_clipboard()
            try:
                nonce = self.send_query(line)
                parts: list[str] = []
                for msg in self.read_responses(nonce, timeout):
                    try:
                        parts.append(msg.data.decode("utf-8"))
                    except UnicodeDecodeError:
                        import base64
                        parts.append(
                            f"<binary {len(msg.data)} bytes, base64>\n"
                            + base64.b64encode(msg.data).decode("ascii")
                        )
                return "\n".join(parts)
            finally:
                self._restore_user_clipboard(saved)

    def _snapshot_user_clipboard(self) -> str | None:
        """Capture the user's clipboard (if any) before the channel co-opts it (§5)."""
        if not self.cfg.restore_user_clipboard:
            return None
        cur = self.t.cb.get_text()
        if cur is None or codec.normalize(cur).startswith(codec.MAGIC):
            return None  # empty, or a leftover CBP frame — nothing worth restoring
        return cur

    def _restore_user_clipboard(self, saved: str | None) -> None:
        if saved is None:
            return
        self.t.cb.set_text(saved)
        self.t._last_raw = codec.normalize(saved)
