# VDI Data-Channel — Technical Specification

## 0. Purpose & audience

This document specifies a **request/response data channel** to a helper process running
*inside* a remote desktop session (Citrix, Windows 365, RDP-style), across a boundary that
normally blocks integration (no network path, no file share, restricted or absent clipboard).

Scope is deliberately narrowed to the **data path only**: getting a small request *into* the
session and pulling a result (potentially large, potentially binary) *out*. It does **not**
cover driving GUI applications by synthetic mouse/keyboard, OCR of arbitrary app screens, or
element detection. Those belong to a separate perception/action spec and are explicitly out
of scope here.

The system supports **two physical transports** behind one protocol:

- **Clipboard transport (CBP)** — for sessions where clipboard text sync works. Bidirectional,
  lossless, the happy path. This is the primary deliverable.
- **Keyboard+QR transport** — for locked-down sessions with no clipboard. Keyboard in, QR out.
  Lossy physical layer, heavier framing. Fallback path.

Both implement the same request/response abstraction and talk to the same in-session helper,
so the helper logic, command set, compression, and local caller are identical across both.

This spec is written for a coding agent to implement. Where a concrete library is named it is
a recommended default, not a mandate.

---

## 1. Scoping assumptions (confirm or override)

Two design points were left open during design discussion. This spec commits to the stronger
option for each; both are called out here so they can be overridden cheaply.

1. **Helper runs as a persistent REPL**, started once per session (via login script, manual
   launch, or a one-time bootstrap), then stays resident reading requests and emitting
   responses across many exchanges. *Rationale:* multi-response streaming and clean
   turn-taking are natural in a resident loop and awkward in a one-shot-per-command model
   (which reintroduces per-launch readiness races). *If overridden to one-shot:* each
   exchange must begin with a readiness handshake and the helper must re-establish transport
   state on every launch; §7 notes the deltas.

2. **VDI #1 (no clipboard) inbound is keyboard-only.** The remote cannot see the local
   screen, so requests must arrive as keystrokes typed into the helper. *If a non-keyboard
   inbound path exists* (a polled dropfile, a socket) the keyboard slice of the QR transport
   can be dropped.

Everything else in this spec is independent of these two choices.

---

## 2. Architecture

```
┌───────────────────────────────────────────────┐
│  LOCAL DRIVER (this host)                       │
│                                                 │
│  caller ──► Channel API (send_query /           │
│             read_responses)                     │
│                     │                           │
│             ┌───────▼────────┐                  │
│             │  Transport     │  ◄── one interface, two impls
│             │  interface     │                  │
│             └───┬────────┬───┘                  │
│      ClipboardTransport  KeyboardQRTransport     │
│         (CBP)            (keyboard in / QR out)  │
└───────────┬──────────────────┬──────────────────┘
            │ clipboard sync    │ keystrokes down / pixels up
┌───────────▼──────────────────▼──────────────────┐
│  REMOTE SESSION                                  │
│             ┌────────────────┐                   │
│             │  Transport     │  (mirror impls)   │
│             │  interface     │                   │
│             └───────┬────────┘                   │
│                     │                            │
│             ┌───────▼────────┐                   │
│             │  HELPER REPL   │  handle(request)  │
│             │  command set   │  → generator of   │
│             │  (grep/read/…) │    responses      │
│             └────────────────┘                   │
└──────────────────────────────────────────────────┘
```

The **helper's `handle()` is a generator**: it `yield`s one or more logical results per
request. The transport turns each yield into a protocol message (framing, chunking,
handshaking per frame). The local `read_responses()` mirrors it as an iterator, so a caller
can begin consuming message 1 while the helper is still computing message 3.

**Module boundaries:**

| Module | Responsibility | Must not do |
|---|---|---|
| `channel` | Public API, exchange orchestration, reassembly, decode | Know which transport is active |
| `transport` | Interface + `ClipboardTransport` + `KeyboardQRTransport` | Business logic, command semantics |
| `codec` | Framing, base64, compression, CRC, chunk split/join | Transport I/O |
| `helper` | Resident REPL, command dispatch, response generators | Transport internals |
| `commands` | The actual in-session operations (grep, read, exec…) | Framing/transport |

---

## 3. The protocol: CBP (Clipboard Protocol)

CBP is defined over a **single shared text slot with no notifications and no framing** — the
clipboard. Every property below follows from that constraint. The QR transport (§6) reuses
the same frame structure and state machine; only the physical read/write differs.

### 3.1 Frame format

One header line, a newline, then the payload:

```
CBP/1 <TYPE> <NONCE> <MSG> <SEQ>/<TOTAL> <ENC> <COMP> <LEN> <CRC32> <FLAGS>
<payload>
```

Header fields are single-space-delimited, fixed order. Payload is everything after the first
`\n`.

| Field | Type | Meaning |
|---|---|---|
| `CBP/1` | literal | Magic + version. Reject anything not starting with this → ignores unrelated clipboard traffic from real users/apps. |
| `TYPE` | enum | `REQ` `RSP` `ACK` `ERR` `FIN` `IDLE`. See §3.3. |
| `NONCE` | hex(≥8) | Minted by the requester; binds an entire exchange. Frames with an unexpected nonce are ignored. |
| `MSG` | uint | Logical response index within the exchange. `0` for REQ/FIN/IDLE. `1,2,3…` for successive responses — **the multi-response axis.** |
| `SEQ`/`TOTAL` | uint/uint | Chunk index within one message. `1/1` = unchunked. `TOTAL=0` = streaming, count unknown, terminated by the `END` flag. |
| `ENC` | `A`\|`B` | Content marker. `A` = ASCII/UTF-8 plain text payload. `B` = binary, base64-encoded payload. |
| `COMP` | `-`\|`Z`\|`G` | Compression of the *original bytes* before encoding: none / zstd / gzip. Orthogonal to `ENC`. |
| `LEN` | uint | Byte length of the payload **as it appears in this frame** (post-encoding). Truncation / partial-sync guard. |
| `CRC32` | hex | crc32 of this frame's payload bytes. Integrity guard. |
| `FLAGS` | csv\|`-` | `MORE` (another message follows this one), `END` (final frame of the exchange). |

### 3.2 ENC / COMP — the ascii-vs-binary rule

`ENC` describes the **wire form**; `COMP` a transform *underneath* it. Sender pipeline:

```
original bytes
  → (optional compress: zstd/gzip)          → sets COMP
  → if result is not clipboard-safe printable UTF-8: base64   → sets ENC=B
  → frame
```

Receiver decode is fully determined by the header — no guessing:

```python
data = payload_bytes
if ENC == 'B':  data = base64_decode(data)
if COMP == 'Z': data = zstd_decompress(data)
elif COMP == 'G': data = gzip_decompress(data)
# caller decides, from command context, whether to UTF-8-decode `data` to text
```

Consequences worth stating explicitly:

- Plain text, uncompressed → `ENC=A COMP=-`, payload is literal text.
- Text, compressed → compression yields binary → must base64 → `ENC=B COMP=Z`.
- Genuine binary (image, blob, protobuf) → `ENC=B` (`COMP=Z` if also compressed).

**Sender rule of thumb:** use `A` only when payload is guaranteed clipboard-safe printable
UTF-8 with no framing-ambiguous characters; otherwise `B`. When in doubt, `B` — base64 costs
~4/3 bloat for total reliability.

### 3.3 Exchange state machine

An **exchange** = one `REQ` plus everything the responder returns under the same nonce. It
contains one or more **messages** (`MSG=1,2,…`), each optionally **chunked** (`SEQ/TOTAL`).
Discipline is **stop-and-wait**: every `RSP` frame is `ACK`'d before the next is written
(mandatory for the clipboard's single slot; §6 notes the QR variant).

```
LOCAL (requester)                          REMOTE (helper)
─────────────────                          ───────────────
write REQ  nonce=N msg=0             ──►    poll: REQ, new nonce N → consume
poll for RSP N …                           run handle(request) generator
                                    ◄──     write RSP N msg=1 seq=1/2  FLAGS -
verify LEN+CRC, read
write ACK N msg=1 seq=1/2           ──►     poll: ACK 1/2 → advance
                                    ◄──     write RSP N msg=1 seq=2/2  FLAGS MORE
reassemble msg 1
write ACK N msg=1 seq=2/2           ──►
                                    ◄──     write RSP N msg=2 seq=1/1  FLAGS END
read msg 2 (final)
write FIN N                         ──►     poll: FIN → exchange done
                                    ◄──     write IDLE   (scrub slot)
```

- **Multi logical responses** (progress + final, or a stream): responder emits `MSG=1` with
  `FLAGS=MORE`, then `MSG=2`, … `MORE` on a message's last frame = "another message follows."
  Absence of `MORE` together with `END` = "that was the last message; exchange complete."
- **One big response**: single message, `SEQ=1/…N`; local reassembles in order, ACKing each.
- **Both compose:** a large streamed result = several messages, each several chunks.
- **Termination is unambiguous:** `END` marks the final frame; `ERR` (payload = error code +
  text) also terminates. After `FIN`, responder writes `IDLE` so a stale `RSP` can never be
  re-read as fresh.

### 3.4 The three races and their guards

1. **Stale-slot read** — a poll may see an old value. Guard: act only when `(TYPE, NONCE)`
   match what this side is waiting for, and `(nonce,msg,seq)` is one not yet seen. `IDLE`
   after each exchange scrubs the slot.
2. **Partial sync / truncation** — some VDI clipboard bridges propagate a large paste
   slightly asynchronously; a poll can catch a frame mid-sync. Guard: verify `LEN` == actual
   payload byte count **and** `CRC32`. On mismatch, **do not ACK** — re-poll; the value
   settles and reads clean next tick. After `M` failed re-polls (default 10), raise a
   transport error.
3. **Duplicate read** — both sides poll, so the same frame may be seen twice before the peer
   advances. Dedupe on `(nonce,msg,seq)`; `ACK` once; ignore repeats. **ACKs are idempotent.**

### 3.5 Polling

Both sides poll the clipboard on a fixed interval (default 50–100 ms; configurable). Each side
reads, checks magic+type+nonce+dedupe, acts, and on any non-matching value simply waits for
the next tick. There is no notification primitive; polling is the only portable mechanism and
is cheap for text.

### 3.6 Chunk sizing & the clipboard cap

Many clipboard bridges cap payload size (sometimes silently truncating). Procedure:

- **Discover the cap empirically at startup:** binary-search a self-test payload (write →
  read back → compare CRC) until round-trips stop matching. Cache the discovered ceiling `C`.
- **Compress before chunking** — cuts frame count *and* sync latency on large results.
- **Size chunks under the cap accounting for base64 inflation:** a `B`-encoded chunk's raw
  size ceiling ≈ `0.75 · C · safety` (default safety 0.9). Header bytes count against `C` too.

### 3.7 Reference frames

```
CBP/1 REQ 9c1f0a3e 0 1/1 A - 22 5a1b2e40 -
get_log ERROR limit=500

CBP/1 RSP 9c1f0a3e 1 1/1 A - 30 88c2f019 MORE
scanning 4 files, 3 matches

CBP/1 RSP 9c1f0a3e 2 1/3 B Z 5980 1af0c7d2 -
<base64 zstd chunk 1 of message 2>

CBP/1 RSP 9c1f0a3e 2 3/3 B Z 5312 9e77aa10 END
<base64 zstd chunk 3 of message 2>

CBP/1 FIN 9c1f0a3e 0 0/0 - - 0 - -
```

---

## 4. Transport interface

Both transports implement the same interface; `channel` is written against it and never
branches on transport type.

```python
class Transport(Protocol):
    def write_frame(self, frame: bytes) -> None: ...
    # Blocks until the frame is placed on the physical medium
    # (clipboard set / QR rendered+settled / keystrokes sent).

    def read_frame(self, timeout_ms: int) -> Frame | None: ...
    # Polls the medium; returns the next *new, valid* frame
    # (magic ok, CRC ok, LEN ok, not a duplicate) or None on timeout.
    # Truncation/partial-sync retries happen INSIDE this call.

    def scrub(self) -> None: ...
    # Clears the slot / renders IDLE. No-op where not applicable.

    def probe(self) -> TransportCaps: ...
    # Returns { max_payload, direction, lossy, needs_ack } —
    # e.g. clipboard cap from §3.6, or QR version/tile budget.
```

`Frame` is the parsed header + raw payload. Header parse/serialize, base64, compression, CRC,
and chunk split/join live in `codec` and are shared by both transports — a transport only
does physical I/O plus the truncation/settle retry appropriate to its medium.

---

## 5. Clipboard transport (primary)

Implements §3 directly.

- **Local I/O:** OS clipboard API — `win32clipboard` (pywin32), `pyperclip`, or platform
  equivalents. Use `CF_UNICODETEXT` only; never rich/HTML/file formats (they sync
  inconsistently across Citrix/RDP versions). Binary always travels as base64 text.
- **Remote I/O:** `Get-Clipboard`/`Set-Clipboard` (PowerShell) or `win32clipboard` (Python)
  inside the session.
- **`needs_ack = true`** — stop-and-wait per §3.3, because the single slot cannot hold frame
  `n+1` until frame `n` is consumed.
- **Global-slot awareness:** a real user or another app in the session shares this clipboard.
  The `CBP/1` magic + nonce make collisions safe (foreign values are ignored), but the helper
  should avoid clobbering a user's clipboard needlessly — restore prior contents on `IDLE`
  where feasible, and document that the channel co-opts the clipboard while active.

---

## 6. Keyboard+QR transport (fallback)

Same frames, same state machine, different physical layer and one flow-control change.

- **Inbound (local → remote):** the serialized `REQ`/`ACK`/`FIN` frame is **typed** into the
  helper via synthetic keystrokes (scan-code `SendInput`, inter-key pacing). Keep inbound
  frames small (requests and ACKs are tiny, so this is fine). Guard keyboard-layout hazards
  (Umlaute, `@`, `\`, `|`).
- **Outbound (remote → local):** the helper **renders** each `RSP` frame as one or more QR
  codes into a **fixed screen rectangle**; local captures that rect and decodes.
  - **`lossy = true`.** VDI display codecs corrupt QR under motion. Render **static**, hold
    still, and gate decode on a **stable-frame detector** (tile-hash idle) so the frame has
    settled to lossless before decoding.
  - **Encoding:** `segno` → raw numpy nearest-neighbor blit at integer module size (6–8 px),
    full quiet zone, ECC level Q/H. **Decoding:** `zxing-cpp` primary; `pyzbar`/OpenCV
    fallback. Crop to the fixed rect + grayscale before decode.
- **Flow control:** stop-and-wait works but is slow (settle-time per frame). Two upgrades,
  both preserving CBP semantics:
  - **Spatial multiplexing** — tile N QR codes (distinct `seq`s) in the rect, decode all per
    capture. Multiplies throughput at constant reliability. Preferred first upgrade.
  - **Fountain coding** — for large/streaming messages, emit rateless LT/raptor-coded frames
    (loop until the local side signals `enough`), so lost frames cost only a few extras and
    no per-frame ACK round-trip is needed. Maps onto CBP as a message whose `TOTAL=0` stream
    is fountain-coded; the `END`/`enough` signal terminates it.
- **`probe()`** returns the QR version, module size, tile count, and whether fountain mode is
  enabled.

**Do not use QR as a bulk file pipe.** Realistic throughput is tens of KB/s after settle
times and ECC. The correct response to "payload too big for QR" is §8 (filter/compress
remote-side), not more frames.

---

## 7. The helper (in-session REPL)

A resident process (§1 assumption 1). Loop:

```
loop:
    frame = transport.read_frame(timeout)
    if frame is REQ and nonce is new:
        request = decode(frame)                 # per §3.2
        try:
            for i, result in enumerate(handle(request), start=1):
                last = result.is_final
                send_response(nonce, msg=i, result, last=last)
                                                # frames, chunks, ACK-waits internally
        except CommandError as e:
            send_error(nonce, e)                # ERR frame, terminates
        finalize(nonce)                         # await FIN, then scrub → IDLE
```

- **`handle(request)` is a generator** — each `yield` is one logical message. Yield progress
  lines first, final data last; the transport handles `MORE`/`END`, chunking, and per-frame
  ACKs. This is what makes multi-response first-class rather than bolted on.
- **`send_response`** applies the §3.2 pipeline (compress? → encode? → chunk under cap → frame
  → write → await ACK per frame for ack-needing transports).
- **One-shot override (§1):** if the helper is launched per-command, wrap the loop body in a
  bootstrap that (a) signals readiness, (b) reads exactly one REQ, (c) streams the response,
  (d) exits. Local side must then launch + await-ready before each exchange.

### 7.1 Command interface (starter set)

Requests are a simple verb + args line (or JSON). The command set is where the "push the
query into the remote" principle lives — the helper does the work in-session and returns only
the result.

| Command | Args | Returns |
|---|---|---|
| `ping` | — | `pong` + helper version, discovered clipboard cap |
| `exec` | shell/PowerShell string | stdout/stderr (streamed as progress messages + final) |
| `read` | path, offset, limit | file window (`ENC=B` if binary) |
| `grep` | path, pattern, context, max | matching lines only (in-session ripgrep) |
| `stat` | path | size, mtime, type |
| `get` | path | whole file, compressed+chunked (`ENC=B COMP=Z`) — bounded by a max-size guard |
| `ls` | path, glob | directory listing |

**Design intent:** callers should prefer `grep`/`read`/`stat` (return a slice) over `get`
(return the blob). The channel is a needle-delivery mechanism, not a file transfer pipe.

---

## 8. Payload larger than one frame — strategy order

Applied by the helper before it ever chunks, in this order:

1. **Filter/aggregate/paginate remote-side.** The biggest lever. Don't return the 500 MB
   log — return the 50 matching lines. Most "too big" cases disappear here.
2. **Compress** (zstd default). Text/logs/JSON compress 5–10×; a 20 KB result becomes ~3 KB.
3. **Chunk** under the transport cap (§3.6) — sequential `SEQ/TOTAL`, lossless reassembly.
   No fountain codes needed on the clipboard (it's lossless); QR uses fountain for large
   streams (§6).
4. **Still massive?** That's a signal to reduce further remote-side, not to transport it.
   Surface a `payload_too_large` error suggesting a narrower query.

| Situation | Approach |
|---|---|
| Caller controls what's emitted | Always filter + compress first |
| Fits one frame after that | Single message, done |
| A few frames | Chunked message, per-frame integrity (clipboard) / multiplex (QR) |
| Large / streaming | Multiple messages; QR → fountain, clipboard → sequential chunks |
| Truly massive (many MB) | Reject with `payload_too_large`; narrow the query |

---

## 9. Local channel API

```python
send_query(payload, enc='A'|'B', comp='-') -> nonce
read_responses(nonce, timeout) -> Iterator[Message]
    # yields each reassembled, integrity-checked, decoded message as it completes;
    # streaming-friendly (msg 1 available before msg 3 exists).
    # terminates when a message arrives with END (or on ERR / timeout).
```

Higher-level convenience wrappers over the command set (`remote_grep(...)`,
`remote_read(...)`) build the request line, call `send_query`, and adapt `read_responses`
(e.g. collapsing progress messages, returning the final payload). These are thin and live in
`channel`, not `transport`.

---

## 10. Error taxonomy

Every failure surfaces a machine-readable code, never a bare string:

`transport_timeout`, `crc_mismatch`, `len_mismatch`, `truncated_retry_exhausted`,
`nonce_mismatch`, `payload_too_large`, `clipboard_unavailable`, `qr_decode_failed`,
`helper_not_ready`, `command_error`, `unsupported_encoding`.

`ERR` frames carry `command_error` (helper-side command failure, payload = message).
Transport-layer failures are raised locally by the transport/codec.

---

## 11. Configuration

```toml
[transport]
kind = "clipboard"          # clipboard | keyboard_qr
poll_interval_ms = 75
truncation_retries = 10

[clipboard]
format = "unicode_text"
probe_cap_on_start = true   # empirical cap discovery (§3.6)
restore_user_clipboard = true

[keyboard_qr]
qr_rect = [40, 40, 900, 900]   # fixed screen rect for QR out
module_px = 8
ecc = "Q"
multiplex_tiles = 4
fountain = false
interkey_delay_ms = [20, 50]

[codec]
compress = "zstd"           # zstd | gzip | none
compress_min_bytes = 512    # below this, don't bother
chunk_safety = 0.9

[helper]
mode = "repl"               # repl | oneshot
max_get_bytes = 8388608     # guard for the `get` command
```

---

## 12. Build order

**M1 — Codec + clipboard happy path.** `codec` (frame parse/serialize, base64, CRC,
zstd, chunk split/join), `ClipboardTransport` with cap probe + truncation guard, the CBP
state machine for single-message unchunked exchanges, `ping`/`grep`/`read` commands, local
`send_query`/`read_responses`. DoD: local `grep` of a 500 MB in-session log returns the
matching lines losslessly over the clipboard, integrity-checked, no network.

**M2 — Multi-response + chunking.** Generator-based `handle`, `MSG` streaming with
`MORE`/`END`, `SEQ/TOTAL` chunking under the probed cap, dedupe + idempotent ACK, `ERR`
path, `IDLE` scrub + optional user-clipboard restore. DoD: a streamed multi-message response
with a chunked binary (`ENC=B COMP=Z`) final message reassembles correctly under induced
duplicate/stale reads.

**M3 — QR/keyboard fallback.** `KeyboardQRTransport` (segno render + stable-frame gate +
zxing-cpp decode; scan-code inbound), behind the same interface. Stop-and-wait first, then
spatial multiplexing. DoD: same exchanges as M1–M2 run unmodified over QR by config switch.

**M4 — Throughput + robustness.** Fountain mode for large QR streams, empirical cap tuning,
`stats`/self-test, one-shot helper mode.

---

## 13. Non-goals

GUI automation (mouse/click/OCR-of-apps), driving the session UI, multi-session, rich
clipboard formats, and bulk multi-MB file transfer. The channel is a request/response
needle-delivery mechanism; anything that wants the whole haystack should reduce it
remote-side instead.
