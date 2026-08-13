"""Arrow-key menu widgets rendered with rich.

Conventions follow common terminal tools (raspi-config, lazygit, htop):
single-select menus move with ↑/↓ (or j/k) and confirm with Enter; multi-select
lists toggle with Space, select-all with ``a``, select-none with ``n``; Esc,
``q`` or Backspace go back; Ctrl-C quits the whole program. Destructive actions
are never inside the widget itself — callers confirm separately.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from shakepie_terminal.keyio import KeyType, read_key

CURSOR = "❯"  # noqa: RUF001 - deliberate prompt glyph
CHECKED = "[●]"
UNCHECKED = "[ ]"
STYLE_CURSOR = "bold cyan"
STYLE_TITLE = "bold"


@dataclass(slots=True)
class MenuItem:
    label: str
    value: str
    hint: str = ""
    danger: bool = False


@dataclass(slots=True)
class ToggleItem:
    label: str
    value: str
    meta: str = ""
    checked: bool = field(default=False)


def _footer(single: bool) -> Text:
    if single:
        return Text("↑/↓ move · Enter select · q/Esc back · Ctrl-C quit", style="dim")
    return Text(
        "↑/↓ move · Space toggle · a all · n none · Enter confirm · q/Esc back",
        style="dim",
    )


def _draw(
    console: Console,
    title: str,
    subtitle: str | None,
    rows: list[RenderableType],
    cursor: int,
    footer: Text,
) -> None:
    console.clear()
    table = Table.grid(padding=(0, 2))
    table.add_column(width=2)
    table.add_column()
    for index, row in enumerate(rows):
        marker = CURSOR if index == cursor else " "
        table.add_row(Text(marker, style=STYLE_CURSOR), row)
    body: RenderableType = table
    if subtitle:
        body = Group(Text(subtitle, style="dim"), table)
    console.print(Panel(body, title=Text(title, style=STYLE_TITLE), border_style="dim"))
    console.print(footer)


class Menu:
    """Single-select arrow-key menu. Returns the chosen value, or None to go back."""

    def __init__(
        self,
        title: str,
        items: list[MenuItem],
        *,
        console: Console,
        key_reader: KeyType | None = None,
        subtitle: str | None = None,
    ) -> None:
        if not items:
            raise ValueError("a menu needs at least one item")
        self.title = title
        self.items = items
        self.console = console
        self.key_reader: KeyType = key_reader or read_key
        self.subtitle = subtitle

    def _render(self, cursor: int) -> None:
        rows: list[RenderableType] = []
        for index, item in enumerate(self.items):
            style = STYLE_CURSOR if index == cursor else ("red" if item.danger else "")
            label = Text(item.label, style=style) if style else Text(item.label)
            if item.hint:
                label.append(f"  {item.hint}", style="dim")
            rows.append(label)
        _draw(self.console, self.title, self.subtitle, rows, cursor, _footer(True))

    def run(self) -> str | None:
        cursor = 0
        while True:
            self._render(cursor)
            key = self.key_reader()
            if key in {"up", "k"}:
                cursor = (cursor - 1) % len(self.items)
            elif key in {"down", "j"}:
                cursor = (cursor + 1) % len(self.items)
            elif key == "home":
                cursor = 0
            elif key == "end":
                cursor = len(self.items) - 1
            elif key == "enter":
                return self.items[cursor].value
            elif key in {"escape", "q", "backspace", "ctrl-d"}:
                return None
            elif key in {"ctrl-c", "\x03"}:
                raise KeyboardInterrupt
            # Redraw in place: move the cursor back above the rendered block.


class MultiSelect:
    """Toggle list with aggregate feedback. Returns checked values, or None to go back."""

    def __init__(
        self,
        title: str,
        items: list[ToggleItem],
        *,
        console: Console,
        key_reader: KeyType | None = None,
        subtitle: str | None = None,
        summary: Callable[[list[ToggleItem]], str] | None = None,
    ) -> None:
        if not items:
            raise ValueError("a multi-select needs at least one item")
        self.title = title
        self.items = items
        self.console = console
        self.key_reader: KeyType = key_reader or read_key
        self.subtitle = subtitle
        self.summary = summary

    def _render(self, cursor: int) -> None:
        rows: list[RenderableType] = []
        for index, item in enumerate(self.items):
            style = STYLE_CURSOR if index == cursor else ""
            marker = CHECKED if item.checked else UNCHECKED
            content = f"{marker} {item.label}"
            label = Text(content, style=style) if style else Text(content)
            if item.meta:
                label.append(f"  {item.meta}", style="dim")
            rows.append(label)
        if self.summary is not None:
            rows.append(Text(self.summary(self.items), style="bold"))
        _draw(self.console, self.title, self.subtitle, rows, cursor, _footer(False))

    def run(self) -> list[str] | None:
        cursor = 0
        while True:
            self._render(cursor)
            key = self.key_reader()
            if key in {"up", "k"}:
                cursor = (cursor - 1) % len(self.items)
            elif key in {"down", "j"}:
                cursor = (cursor + 1) % len(self.items)
            elif key == "space":
                self.items[cursor].checked = not self.items[cursor].checked
            elif key == "a":
                for item in self.items:
                    item.checked = True
            elif key == "n":
                for item in self.items:
                    item.checked = False
            elif key == "enter":
                return [item.value for item in self.items if item.checked]
            elif key in {"escape", "q", "backspace", "ctrl-d"}:
                return None
            elif key in {"ctrl-c", "\x03"}:
                raise KeyboardInterrupt
