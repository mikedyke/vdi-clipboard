"""Physical clipboard I/O (§5).

Uses ``CF_UNICODETEXT`` only — never rich/HTML/file formats, which sync
inconsistently across Citrix/RDP versions. Binary always travels as base64 text.
Prefers pywin32 on Windows; falls back to pyperclip elsewhere / on failure.
"""

from __future__ import annotations

from .errors import ClipboardUnavailable

_backend = None


def _init_backend():
    global _backend
    if _backend is not None:
        return _backend
    try:  # Windows, unicode text slot
        import win32clipboard  # type: ignore
        import win32con  # type: ignore

        def _get():
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                return None
            finally:
                win32clipboard.CloseClipboard()

        def _set(text: str):
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()

        _backend = ("win32", _get, _set)
        return _backend
    except Exception:
        pass

    try:  # portable fallback
        import pyperclip  # type: ignore

        _backend = ("pyperclip", lambda: pyperclip.paste(), lambda t: pyperclip.copy(t))
        return _backend
    except Exception as e:  # pragma: no cover
        raise ClipboardUnavailable(str(e))


class InMemoryClipboard:
    """A thread-safe in-process clipboard slot.

    Share one instance between two transports to model the single shared slot
    without touching the OS clipboard — used by the deterministic test suite / CI
    where a real clipboard (window station) may be unavailable.
    """

    def __init__(self):
        import threading
        self.name = "memory"
        self._lock = threading.Lock()
        self._value: str | None = None

    def get_text(self) -> str | None:
        with self._lock:
            return self._value or None

    def set_text(self, text: str) -> None:
        with self._lock:
            self._value = text


class Clipboard:
    """Thin, retrying wrapper around the OS clipboard text slot."""

    def __init__(self):
        self.name, self._get, self._set = _init_backend()

    def get_text(self) -> str | None:
        try:
            val = self._get()
        except Exception:
            return None  # transient OpenClipboard contention — treat as "no new value"
        return val if val else None

    def set_text(self, text: str) -> None:
        last = None
        for _ in range(20):  # clipboard can be briefly locked by another process
            try:
                self._set(text)
                return
            except Exception as e:  # pragma: no cover
                last = e
        raise ClipboardUnavailable(str(last))
