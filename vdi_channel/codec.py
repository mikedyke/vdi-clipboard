"""Codec (§3.1, §3.2, §3.6): framing, base64, compression, CRC, chunk split/join.

Knows nothing about transport I/O. Shared verbatim by both the local channel
and the in-session helper so their wire formats are identical.

Wire hardening note (Windows clipboards):
Payloads are constrained so the *only* newline in a serialized frame is the
single header/payload separator. ``ENC=A`` is used only for text with no CR/LF;
anything containing newlines (e.g. multi-line command output) becomes ``ENC=B``
base64, which has no newlines. On read we normalize CRLF->LF and strip clipboard-
appended trailing newlines, so LEN/CRC computed by the sender still match.
"""

from __future__ import annotations

import base64
import gzip
import zlib
from dataclasses import dataclass, field

from .errors import CrcMismatch, LenMismatch, UnsupportedEncoding

try:  # zstd is the default per §10; gzip is the stdlib fallback
    import zstandard as _zstd
except Exception:  # pragma: no cover
    _zstd = None

MAGIC = "CBP/1"

# TYPEs (§3.3)
REQ, RSP, ACK, ERR, FIN, IDLE = "REQ", "RSP", "ACK", "ERR", "FIN", "IDLE"
CONTROL_TYPES = {ACK, FIN, IDLE}

# FLAGS (§3.1)
MORE, END = "MORE", "END"


# --------------------------------------------------------------------------- #
# Frame
# --------------------------------------------------------------------------- #
@dataclass
class Frame:
    type: str
    nonce: str
    msg: int = 0
    seq: int = 1
    total: int = 1
    enc: str = "-"           # A | B | -
    comp: str = "-"          # - | Z | G
    payload: bytes = b""     # wire form (post-encoding), exactly as it appears in the frame
    flags: set = field(default_factory=set)

    def has(self, flag: str) -> bool:
        return flag in self.flags

    def to_text(self) -> str:
        crc = "-" if not self.payload else f"{zlib.crc32(self.payload) & 0xFFFFFFFF:08x}"
        flags = ",".join(sorted(self.flags)) if self.flags else "-"
        header = (
            f"{MAGIC} {self.type} {self.nonce} {self.msg} "
            f"{self.seq}/{self.total} {self.enc} {self.comp} "
            f"{len(self.payload)} {crc} {flags}"
        )
        if not self.payload:
            return header
        return header + "\n" + self.payload.decode("utf-8")


def normalize(raw: str) -> str:
    """Undo clipboard line-ending mangling before parsing/deduping."""
    return raw.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def parse(raw: str) -> Frame:
    """Parse a normalized clipboard string into a Frame, validating LEN and CRC.

    Raises LenMismatch / CrcMismatch on a partial/mangled read so the transport
    can re-poll (§3.4 race #2). Assumes ``raw`` has already been normalized.
    """
    if "\n" in raw:
        header, payload_text = raw.split("\n", 1)
    else:
        header, payload_text = raw, ""

    parts = header.split(" ")
    if len(parts) != 10 or parts[0] != MAGIC:
        raise LenMismatch("bad header")

    _, typ, nonce, msg, seqtotal, enc, comp, length, crc, flags = parts
    seq_s, total_s = seqtotal.split("/", 1)
    payload = payload_text.encode("utf-8")

    if int(length) != len(payload):
        raise LenMismatch(f"declared {length}, got {len(payload)}")
    if crc != "-" and int(crc, 16) != (zlib.crc32(payload) & 0xFFFFFFFF):
        raise CrcMismatch("payload crc32 mismatch")

    flag_set = set() if flags == "-" else set(flags.split(","))
    return Frame(
        type=typ, nonce=nonce, msg=int(msg), seq=int(seq_s), total=int(total_s),
        enc=enc, comp=comp, payload=payload, flags=flag_set,
    )


# --------------------------------------------------------------------------- #
# Control-frame constructors
# --------------------------------------------------------------------------- #
def make_ack(nonce: str, msg: int, seq: int, total: int) -> Frame:
    return Frame(ACK, nonce, msg=msg, seq=seq, total=total)


def make_fin(nonce: str) -> Frame:
    return Frame(FIN, nonce, msg=0, seq=0, total=0)


def make_idle(nonce: str = "0" * 8) -> Frame:
    return Frame(IDLE, nonce, msg=0, seq=0, total=0)


# --------------------------------------------------------------------------- #
# ENC / COMP pipeline (§3.2)
# --------------------------------------------------------------------------- #
def _is_clip_safe(data: bytes) -> bool:
    """True if ``data`` is printable UTF-8 with no framing-ambiguous chars.

    Rejects CR/LF (so ENC=A payloads are single-line and survive clipboards) and
    other control chars except TAB.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    for ch in text:
        o = ord(ch)
        if ch in ("\n", "\r"):
            return False
        if o < 0x20 and ch != "\t":
            return False
    return True


def encode_payload(original: bytes, compress: str = "zstd", compress_min: int = 512):
    """original bytes -> (enc, comp, wire_bytes) following the §3.2 sender pipeline."""
    comp = "-"
    body = original
    if compress != "none" and len(original) >= compress_min:
        if compress == "zstd" and _zstd is not None:
            body = _zstd.ZstdCompressor().compress(original)
            comp = "Z"
        else:  # gzip fallback (also when zstd requested but unavailable)
            body = gzip.compress(original)
            comp = "G"
        # If compression didn't help, keep the original bytes uncompressed.
        if len(body) >= len(original):
            body, comp = original, "-"

    if comp != "-":
        return "B", comp, base64.b64encode(body)
    if _is_clip_safe(original):
        return "A", "-", original
    return "B", "-", base64.b64encode(original)


def decode_payload(enc: str, comp: str, wire: bytes) -> bytes:
    """Inverse of encode_payload — fully determined by the header (§3.2)."""
    data = wire
    if enc == "B":
        data = base64.b64decode(_strip_b64(data))
    elif enc not in ("A", "-"):
        raise UnsupportedEncoding(enc)

    if comp == "Z":
        if _zstd is None:
            raise UnsupportedEncoding("zstd not installed")
        data = _zstd.ZstdDecompressor().decompress(data)
    elif comp == "G":
        data = gzip.decompress(data)
    elif comp != "-":
        raise UnsupportedEncoding(comp)
    return data


def _strip_b64(data: bytes) -> bytes:
    # Defensive: drop any stray whitespace a clipboard bridge may have injected.
    return bytes(c for c in data if c not in b" \t\r\n")


# --------------------------------------------------------------------------- #
# Chunking (§3.6, §7 step 3)
# --------------------------------------------------------------------------- #
def split_chunks(wire: bytes, chunk_max: int) -> list[bytes]:
    if len(wire) <= chunk_max:
        return [wire]
    return [wire[i:i + chunk_max] for i in range(0, len(wire), chunk_max)]
