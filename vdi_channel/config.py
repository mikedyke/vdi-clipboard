"""Configuration defaults (§10). Overridable via env or constructor kwargs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # [transport]
    kind: str = "clipboard"          # clipboard | keyboard_qr (only clipboard implemented)
    poll_interval_ms: int = 75
    truncation_retries: int = 10

    # [clipboard]
    scrub_on_start: bool = True      # clear stale frames from a prior run before serving
    probe_cap_on_start: bool = True  # empirical cap discovery (§3.6)
    probe_cap_max: int = 1_000_000   # upper bound for the binary search
    default_cap: int = 100_000       # used when probing is disabled/fails
    restore_user_clipboard: bool = False

    # [codec]
    compress: str = "zstd"           # zstd | gzip | none
    compress_min_bytes: int = 512    # below this, don't bother compressing
    chunk_safety: float = 0.9
    header_reserve: int = 160        # bytes reserved for the frame header per chunk

    # [helper]
    mode: str = "repl"               # repl | oneshot (repl implemented)
    max_get_bytes: int = 8 * 1024 * 1024

    # channel-level
    request_timeout_s: float = 60.0

    # Per-frame handshake timeouts. Same-machine testing sees sub-second round
    # trips, but a real Citrix/RDP clipboard-redirection hop can take much
    # longer and more variably to sync a value across the VDI boundary. Bump
    # these (VDI_ACK_TIMEOUT_S / VDI_CLOSE_TIMEOUT_S) if a real deployment sees
    # "no ACK"/"no FIN" transport_timeout warnings under normal (non-duplicate-
    # helper) operation.
    ack_timeout_s: float = 60.0   # helper: how long to wait for ACK/FIN from the requester
    close_timeout_s: float = 30.0  # local: how long to wait for the helper's IDLE after FIN

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        """Build a Config, letting ``VDI_*`` env vars override the defaults."""
        cfg = cls(**overrides)
        for f in cfg.__dataclass_fields__:
            env = os.environ.get(f"VDI_{f.upper()}")
            if env is None:
                continue
            cur = getattr(cfg, f)
            try:
                if isinstance(cur, bool):
                    val = env.strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(cur, int):
                    val = int(env)
                elif isinstance(cur, float):
                    val = float(env)
                else:
                    val = env
                setattr(cfg, f, val)
            except ValueError:
                pass
        return cfg

    def chunk_max(self, cap: int) -> int:
        """Max payload bytes per frame given a discovered clipboard cap ``C`` (§3.6)."""
        return max(256, int(cap * self.chunk_safety) - self.header_reserve)
