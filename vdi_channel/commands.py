"""In-session command set (§6.1).

Each command is a generator of ``bytes`` — one ``yield`` per logical message
(the MSG multi-response axis, §3.1). Yield progress first, final data last; the
helper turns yields into MORE/END framing, chunking and compression.

Design intent (§6.1): prefer grep/read/stat (a slice) over get (the blob). The
channel is a needle-delivery mechanism, not a file-transfer pipe. The heavy
lifting happens *in-session* — we push the query to the remote and return only
the result (§7 step 1).
"""

from __future__ import annotations

import glob as _glob
import os
import shlex
import subprocess
import time
from typing import Iterator

from .config import Config
from .errors import CommandError, PayloadTooLarge

VERSION = "vdi-helper/1"


def run_ps(command: str, timeout: float = 300.0) -> bytes:
    """Execute a PowerShell command in-session, returning stdout+stderr bytes."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, timeout=timeout,
        )
    except FileNotFoundError:  # non-Windows dev box — fall back to sh
        proc = subprocess.run(["sh", "-c", command], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CommandError(f"command timed out after {timeout}s")
    out = proc.stdout or b""
    if proc.stderr:
        out += (b"\n" if out else b"") + proc.stderr
    return out


def dispatch(line: str, cfg: Config, cap: int) -> Iterator[bytes]:
    """Parse a request line ``<verb> <args>`` and return the command generator."""
    line = line.strip()
    if not line:
        raise CommandError("empty request")
    verb, _, rest = line.partition(" ")
    verb = verb.lower()

    handler = _COMMANDS.get(verb)
    if handler is None:
        raise CommandError(f"unknown command: {verb}")
    return handler(rest.strip(), cfg, cap)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _ping(rest: str, cfg: Config, cap: int) -> Iterator[bytes]:
    yield f"pong {VERSION} cap={cap} compress={cfg.compress}".encode("utf-8")


def _exec(rest: str, cfg: Config, cap: int) -> Iterator[bytes]:
    if not rest:
        raise CommandError("exec requires a command string")
    preview = rest if len(rest) <= 80 else rest[:77] + "..."
    yield f"[exec] {preview}".encode("utf-8")   # progress message (MSG=1, MORE)
    yield run_ps(rest)                            # final output   (MSG=2, END)


def _read(rest: str, cfg: Config, cap: int) -> Iterator[bytes]:
    args = shlex.split(rest)
    if not args:
        raise CommandError("read requires: <path> [offset] [limit]")
    path = args[0]
    offset = int(args[1]) if len(args) > 1 else 0
    limit = int(args[2]) if len(args) > 2 else 65536
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            yield f.read(limit)
    except OSError as e:
        raise CommandError(str(e))


def _grep(rest: str, cfg: Config, cap: int) -> Iterator[bytes]:
    args = shlex.split(rest)
    if len(args) < 2:
        raise CommandError("grep requires: <path> <pattern> [context] [max]")
    path, pattern = args[0], args[1]
    context = int(args[2]) if len(args) > 2 else 0
    maxn = int(args[3]) if len(args) > 3 else 500
    yield f"[grep] {pattern} in {path}".encode("utf-8")  # progress
    ps = (
        f"Select-String -Path {_q(path)} -Pattern {_q(pattern)} "
        f"-Context {context},{context} | Select-Object -First {maxn} | Out-String"
    )
    yield run_ps(ps)


def _stat(rest: str, cfg: Config, cap: int) -> Iterator[bytes]:
    path = shlex.split(rest)[0] if rest else ""
    if not path:
        raise CommandError("stat requires a path")
    try:
        st = os.stat(path)
    except OSError as e:
        raise CommandError(str(e))
    kind = "dir" if os.path.isdir(path) else "file"
    info = f"path={path}\nsize={st.st_size}\nmtime={int(st.st_mtime)}\ntype={kind}"
    yield info.encode("utf-8")


def _ls(rest: str, cfg: Config, cap: int) -> Iterator[bytes]:
    args = shlex.split(rest)
    path = args[0] if args else "."
    pattern = args[1] if len(args) > 1 else "*"
    try:
        entries = sorted(_glob.glob(os.path.join(path, pattern)))
    except OSError as e:
        raise CommandError(str(e))
    lines = []
    for e in entries:
        tag = "d" if os.path.isdir(e) else "f"
        try:
            size = os.path.getsize(e)
        except OSError:
            size = 0
        lines.append(f"{tag} {size:>12} {os.path.basename(e)}")
    yield ("\n".join(lines) or "(empty)").encode("utf-8")


def _get(rest: str, cfg: Config, cap: int) -> Iterator[bytes]:
    path = shlex.split(rest)[0] if rest else ""
    if not path:
        raise CommandError("get requires a path")
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise CommandError(str(e))
    if size > cfg.max_get_bytes:
        raise PayloadTooLarge(
            f"{size} bytes exceeds max_get_bytes={cfg.max_get_bytes}; narrow the query"
        )
    with open(path, "rb") as f:
        yield f.read()  # helper compresses+chunks (ENC=B COMP=Z) automatically


def _q(s: str) -> str:
    """Quote a value for PowerShell single-quoted string context."""
    return "'" + s.replace("'", "''") + "'"


_COMMANDS = {
    "ping": _ping,
    "exec": _exec,
    "read": _read,
    "grep": _grep,
    "stat": _stat,
    "ls": _ls,
    "get": _get,
}
