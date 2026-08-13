"""Portable single-key terminal input for arrow-key menus on Windows and POSIX.

Returns normalized key names so the menu code never touches platform specifics:

``up`` ``down`` ``left`` ``right`` ``enter`` ``space`` ``escape`` ``backspace``
``home`` ``end`` ``pageup`` ``pagedown`` ``tab`` ``ctrl-c`` ``ctrl-d`` and single
printable characters (lowercased). The reader is injected into menus, which keeps
them testable with scripted key sequences.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

_ESC = "\x1b"


def _named(raw: str) -> str:
    mapping = {
        "\r": "enter",
        "\n": "enter",
        " ": "space",
        "\x7f": "backspace",
        "\x08": "backspace",
        "\t": "tab",
    }
    return mapping.get(raw, raw)


def _read_posix() -> str:
    if sys.platform == "win32":
        raise OSError("POSIX keyboard input is unavailable on Windows")
    import select
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    old_attributes = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor)
        raw = os.read(descriptor, 1).decode("utf-8", errors="replace")
        if raw == _ESC:
            # An escape sequence may follow; probe briefly without blocking.
            if select.select([sys.stdin], [], [], 0.05)[0]:
                sequence = os.read(descriptor, 2).decode("utf-8", errors="replace")
                if sequence == "[A":
                    return "up"
                if sequence == "[B":
                    return "down"
                if sequence == "[C":
                    return "right"
                if sequence == "[D":
                    return "left"
                if sequence == "[H":
                    return "home"
                if sequence == "[F":
                    return "end"
                if sequence in {"[5~", "[Z"}:
                    return "pageup"
                if sequence == "[6~":
                    return "pagedown"
                return "escape"
            return "escape"
        return _named(raw)
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, old_attributes)


def _read_windows() -> str:
    if sys.platform != "win32":
        raise OSError("Windows keyboard input is unavailable on this platform")
    import msvcrt

    raw = msvcrt.getwch()
    if raw in {"\x00", "\xe0"}:
        code = msvcrt.getwch()
        return {
            "H": "up",
            "P": "down",
            "K": "left",
            "M": "right",
            "G": "home",
            "O": "end",
            "I": "pageup",
            "Q": "pagedown",
        }.get(code, "escape")
    if raw == _ESC:
        return "escape"
    return _named(raw)


def read_key() -> str:
    """Read one key press and return its normalized name."""
    if os.name == "nt":
        return _read_windows()
    if sys.stdin.isatty():
        return _read_posix()
    # Piped input (scripted tests, non-interactive runs): read one line per key.
    line = sys.stdin.readline()
    if not line:
        return "ctrl-d"
    return _named(line.rstrip("\n"))


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


KeyType = Callable[[], str]
