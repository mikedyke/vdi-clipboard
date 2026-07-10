# vdi-clipboard

[![CI](https://github.com/mikedyke/vdi-clipboard/actions/workflows/ci.yml/badge.svg)](https://github.com/mikedyke/vdi-clipboard/actions/workflows/ci.yml)

A request/response **data channel into a VDI session over the clipboard**, plus a
**Claude Code MCP server** that drives it. Implements the CBP (Clipboard Protocol)
from [`data-channel-spec.md`](data-channel-spec.md) — clipboard transport (§3, §5),
compression (§3.2), multi-message streaming and chunking (§3.3, §7).

```
Claude Code ──stdio──► MCP server (local driver) ──clipboard──► Helper (in VDI) ──► PowerShell
   tools              vdi_channel.mcp_server        CBP frames    vdi_channel.helper
```

Two processes talk through one shared clipboard text slot:

- **Local driver** — an MCP server (`run_mcp.py`) exposing `vdi_*` tools to Claude Code.
  It mints requests, writes `REQ` frames, and reassembles the responses.
- **In-session helper** — `run_helper.py`, run *inside* the remote desktop. It polls
  the clipboard for `REQ`s, executes each command in an underlying PowerShell, and
  writes the result back as `RSP` frames — **compressed (zstd)**, **chunked** under the
  clipboard cap, and **multi-message** (progress lines then final output).

## Install

The two sides have different dependencies, so the deps are split into extras — pick
the one for the machine you're on:

```powershell
pip install -e ".[server]"    # local host: MCP server + channel (pulls in mcp)
pip install -e ".[client]"    # inside the VDI session: helper only, no mcp
```

Both register the console scripts **`vdi-mcp`** (local driver) and **`vdi-helper`**
(in-session). The **client** extra is deliberately minimal — clipboard access
(`pyperclip`, plus `pywin32` on Windows) and `zstandard`; **`mcp` is not installed on
the client**. gzip is the compression fallback when `zstandard` is absent, and
`pyperclip` the clipboard fallback when `pywin32` is unavailable.

Prefer plain pip? `requirements.txt` is the full/server set and
`requirements-client.txt` the minimal client set:

```powershell
pip install -r requirements-client.txt   # in-session helper only
```

## Run

**Inside the VDI session** (the remote side):

```powershell
vdi-helper          # or: python run_helper.py
```

**On the local host**, register the MCP server with Claude Code. A ready-made
[`.mcp.json`](.mcp.json) is included — or:

```powershell
claude mcp add vdi-clipboard -- python C:/Projects/vdi-clipboard/run_mcp.py
```

Claude Code then has these tools (all run *in-session*, only results cross the wire):

| Tool | Purpose |
|---|---|
| `vdi_ping` | Liveness + helper version + discovered clipboard cap |
| `vdi_exec` | Run a PowerShell command, stream output back |
| `vdi_read` | Read a byte window of a file (`offset`, `limit`) |
| `vdi_grep` | In-session `Select-String`; returns matches only |
| `vdi_stat` | size / mtime / type for a path |
| `vdi_ls`   | Directory listing (optional glob) |
| `vdi_get`  | Whole file, compressed+chunked (bounded by `max_get_bytes`) |

Prefer `grep`/`read`/`stat` (a slice) over `get` (the blob) — the channel is a
needle-delivery mechanism, not a file pipe (§6.1, §12).

## Logging

The helper writes a timestamped audit trail to stderr: for every request it logs the
command (`command <nonce>: <verb> <args>`) and its outcome/duration, and for each
in-session shell execution the command, exit code, duration and output size
(`exec: ...` / `exec done: rc=0 in 237ms, 13B: ...`). Timestamps come from the log
format configured in `vdi-helper`. Redirect stderr to a file to keep the trail:

```powershell
vdi-helper 2>> vdi-helper.log
```

## Testing

The `tests/` suite runs the full protocol over an **in-memory clipboard** — no OS
clipboard or VDI needed, so it's deterministic and CI-safe (this is what the CI
badge above runs, on Linux + Windows):

```powershell
pytest
```

The scripts at the repo root are **manual integration tests** that use the *real* OS
clipboard, plus a quick CLI:

```powershell
python vdi_cli.py ping
python vdi_cli.py exec "Get-Process | Select-Object -First 5"
python test_loopback.py       # helper thread + channel over the real clipboard
python test_mcp.py            # real MCP stdio subprocess + helper thread
python test_interference.py   # a 'user' copying text during live exchanges
```

## How it works (map to the spec)

| Module | Spec | Responsibility |
|---|---|---|
| `codec.py` | §3.1, §3.2, §3.6 | Frame parse/serialize, base64, zstd/gzip, CRC32, chunk split/join |
| `clipboard.py` | §5 | `CF_UNICODETEXT` I/O (pywin32 → pyperclip fallback) |
| `transport.py` | §4, §5 | Physical read/write, `_last_raw` dedupe, truncation retry, cap probe |
| `channel.py` | §3.3, §8 | Requester half: `send_query` / `read_responses`, `request()` |
| `helper.py` | §6 | Responder REPL: poll → dispatch → stream RSP with MORE/END + ACK |
| `commands.py` | §6.1 | `ping`/`exec`/`read`/`grep`/`stat`/`ls`/`get` (PowerShell + filesystem) |
| `mcp_server.py` | §8 | FastMCP tools wrapping `channel.request` |

**Key correctness points** (all clipboard-specific hazards from §3.4, learned the
hard way in testing):

- **Single-slot dedupe.** Each side records the last text it wrote or read; a frame
  is acted on only if it differs, matches the magic + nonce, and passes LEN/CRC.
- **Coexisting with normal clipboard use.** Any value that isn't a `CBP/1` frame — a
  user copying text, another app writing the clipboard — is ignored, never misread as
  a frame. If such a copy lands *mid-exchange* and clobbers an in-flight frame, the
  side awaiting acknowledgment detects the foreign value and **retransmits** its frame
  (`transport.reassert`), so the exchange self-heals instead of stalling. With
  `restore_user_clipboard=true` the user's pre-exchange clipboard content is snapshotted
  and put back when the exchange finishes (the channel still co-opts the slot *while*
  active — see §5).
- **Partial-sync guard.** A frame failing LEN/CRC is treated as a mid-sync read and
  re-polled (up to `truncation_retries`) rather than ACKed.
- **Don't go deaf waiting on a slow/lost FIN.** If a real cross-VDI clipboard round trip
  makes the closing handshake slow or unreliable, the helper's wait for ACK/FIN
  (`_await`) would otherwise ignore any newer REQ that arrives in the meantime and sit
  out the full `ack_timeout_s` before ever looking at it. Instead it abandons the wait
  immediately on seeing a REQ for a different nonce — the requester has clearly moved
  on — so the next request gets served right away instead of after a multi-minute stall.
- **FIN→IDLE close.** The requester waits for the helper's `IDLE` scrub after `FIN`
  before returning, so the next exchange's `REQ` can't clobber the slot mid-handshake.
- **Startup scrub.** The helper clears the slot on boot so a stale `REQ` from a prior
  run can't strand it in a dead exchange.
- **Newline-safe framing.** `ENC=A` is used only for CR/LF-free text; anything with
  newlines (e.g. command output) becomes `ENC=B` base64, so the sole `\n` in a frame
  is the header separator — surviving clipboards that rewrite line endings.

## Configuration

Defaults live in `config.py` (§10) and are overridable via `VDI_*` env vars, e.g.
`VDI_COMPRESS=gzip`, `VDI_POLL_INTERVAL_MS=50`, `VDI_MAX_GET_BYTES=4194304`,
`VDI_PROBE_CAP_ON_START=false`. The MCP server reads them from its environment
(see `.mcp.json`); the helper reads them from the session environment.

**Tuning for a real VDI clipboard boundary.** `poll_interval_ms` only controls how
often each side checks its *own* view of the clipboard — it doesn't govern how fast
Citrix/RDP clipboard redirection actually syncs a value across the session boundary,
which can be far slower and more variable than a same-machine round trip. Two
handshake timeouts bound that: `VDI_ACK_TIMEOUT_S` (helper: how long it waits for the
requester's ACK/FIN, default 60s) and `VDI_CLOSE_TIMEOUT_S` (local: how long it waits
for the helper's IDLE after sending FIN, default 30s). If you see `transport_timeout`
warnings (`no ACK`/`no FIN`) under normal single-helper operation, raise these on
**both** sides — set the env var wherever each process runs, since each only reads its
own environment.

## Scope

Implemented: the **clipboard transport** end to end (spec build order M1–M3 — codec, cap
probe, truncation guard, state machine, multi-response streaming, chunking, compression,
dedupe/idempotent ACK, ERR path, IDLE scrub, retransmit-on-clobber). Explicit non-goals
(§12): rich clipboard formats and bulk multi-MB file transfer — the channel is a
request/response needle-delivery mechanism, not a file pipe.

## License

MIT — see [LICENSE](LICENSE).
