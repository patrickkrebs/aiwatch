# Copyright 2026 Patrick Krebs
# SPDX-License-Identifier: Apache-2.0

"""Key presses on Windows, for the live view. wfwatch reads keys through termios, which only exists
on macOS and Linux; this does the same with the Windows console (msvcrt) and returns the same key
names (tab, enter, esc, up, down, left, right, home, end, pgup, pgdn, shift-tab, or the character)."""

from __future__ import annotations

import sys
import time

_PLAIN = {"\t": "tab", "\r": "enter", "\n": "enter", "\x1b": "esc"}
# After a 0x00 or 0xE0 prefix the console sends one code for a special key.
_SPECIAL = {
    "H": "up", "P": "down", "K": "left", "M": "right",
    "G": "home", "O": "end", "I": "pgup", "Q": "pgdn",
    "\x0f": "shift-tab",
}
_PREFIX = ("\x00", "\xe0")
TICK = 0.01


class Keys:
    """Same interface as wfwatch.keys.Keys: a context manager whose poll(timeout) returns key names."""

    def __init__(self, console=None):
        self.msvcrt = console
        if self.msvcrt is None and sys.stdin.isatty():
            import msvcrt

            self.msvcrt = msvcrt

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def _one(self) -> list[str]:
        ch = self.msvcrt.getwch()
        if ch in _PREFIX:
            name = _SPECIAL.get(self.msvcrt.getwch())
            return [name] if name else []
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in _PLAIN:
            return [_PLAIN[ch]]
        return [ch] if ch >= " " and ch != "\x7f" else []

    def poll(self, timeout: float) -> list[str]:
        """Wait up to ``timeout`` seconds; return the keys pressed."""
        if self.msvcrt is None:
            time.sleep(timeout)
            return []
        end = time.monotonic() + timeout
        keys: list[str] = []
        while True:
            while self.msvcrt.kbhit():
                keys += self._one()
            if keys or time.monotonic() >= end:
                return keys
            time.sleep(TICK)
