"""Unit tests for the codec: framing, ENC/COMP pipeline, chunking, integrity."""
import os

import pytest

from vdi_channel import codec
from vdi_channel.errors import CrcMismatch, LenMismatch


def test_frame_roundtrip_text():
    f = codec.Frame(codec.RSP, "9c1f0a3e", msg=1, seq=1, total=1,
                    enc="A", comp="-", payload=b"scanning 4 files", flags={codec.MORE})
    parsed = codec.parse(codec.normalize(f.to_text()))
    assert parsed.type == codec.RSP
    assert parsed.nonce == "9c1f0a3e"
    assert parsed.payload == b"scanning 4 files"
    assert parsed.has(codec.MORE)


def test_control_frame_has_no_payload():
    fin = codec.make_fin("deadbeef")
    text = fin.to_text()
    assert "\n" not in text
    parsed = codec.parse(codec.normalize(text))
    assert parsed.type == codec.FIN
    assert parsed.payload == b""


def test_len_mismatch_detected():
    good = codec.Frame(codec.RSP, "aa11bb22", payload=b"hello", enc="A").to_text()
    # Corrupt the payload without fixing LEN -> must raise.
    tampered = good[:-1]  # drop a payload byte
    with pytest.raises((LenMismatch, CrcMismatch)):
        codec.parse(codec.normalize(tampered))


def test_crc_mismatch_detected():
    header, payload = codec.Frame(codec.RSP, "aa11bb22", payload=b"hello",
                                  enc="A").to_text().split("\n", 1)
    # Same length, different bytes -> CRC must catch it.
    tampered = header + "\n" + "world"
    with pytest.raises(CrcMismatch):
        codec.parse(codec.normalize(tampered))


def test_plain_text_is_enc_a():
    enc, comp, wire = codec.encode_payload(b"hello world", compress="none")
    assert (enc, comp) == ("A", "-")
    assert wire == b"hello world"
    assert codec.decode_payload(enc, comp, wire) == b"hello world"


def test_multiline_forces_base64():
    # Newlines are framing-ambiguous -> must go base64 (ENC=B), no compression.
    data = b"line1\nline2\r\nline3"
    enc, comp, wire = codec.encode_payload(data, compress="none")
    assert enc == "B"
    assert b"\n" not in wire and b"\r" not in wire
    assert codec.decode_payload(enc, comp, wire) == data


@pytest.mark.parametrize("algo", ["zstd", "gzip"])
def test_compression_roundtrip(algo):
    data = b"ERROR " * 2000  # highly compressible
    enc, comp, wire = codec.encode_payload(data, compress=algo, compress_min=16)
    assert enc == "B"
    assert comp in ("Z", "G")
    assert len(wire) < len(data)
    assert codec.decode_payload(enc, comp, wire) == data


def test_chunk_split_join_reassembles():
    data = os.urandom(10240)  # incompressible -> stays large -> genuinely multi-chunk
    enc, comp, wire = codec.encode_payload(data, compress="zstd", compress_min=16)
    chunks = codec.split_chunks(wire, 500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    rejoined = b"".join(chunks)
    assert codec.decode_payload(enc, comp, rejoined) == data
