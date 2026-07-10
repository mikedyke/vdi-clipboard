"""Error taxonomy (§10). Every failure carries a machine-readable code."""


class ChannelError(Exception):
    """Base for all channel failures. ``code`` is one of the §10 identifiers."""

    code = "channel_error"

    def __init__(self, message: str = "", code: str | None = None):
        if code:
            self.code = code
        super().__init__(f"{self.code}: {message}" if message else self.code)
        self.message = message


class TransportTimeout(ChannelError):
    code = "transport_timeout"


class CrcMismatch(ChannelError):
    code = "crc_mismatch"


class LenMismatch(ChannelError):
    code = "len_mismatch"


class TruncatedRetryExhausted(ChannelError):
    code = "truncated_retry_exhausted"


class NonceMismatch(ChannelError):
    code = "nonce_mismatch"


class PayloadTooLarge(ChannelError):
    code = "payload_too_large"


class ClipboardUnavailable(ChannelError):
    code = "clipboard_unavailable"


class HelperNotReady(ChannelError):
    code = "helper_not_ready"


class CommandError(ChannelError):
    """Helper-side command failure. Travels in an ERR frame."""

    code = "command_error"


class UnsupportedEncoding(ChannelError):
    code = "unsupported_encoding"
