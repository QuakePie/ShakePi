from __future__ import annotations

import pytest
from rich.console import Console

from shakepie_terminal.menu import Menu, MenuItem, MultiSelect, ToggleItem
from shakepie_terminal.names import day_of_file_name, human_bytes, is_finalized_recording_name


def keys(*sequence: str) -> list[str]:
    return list(sequence)


def test_single_menu_moves_and_selects() -> None:
    items = [
        MenuItem("One", "one"),
        MenuItem("Two", "two"),
        MenuItem("Three", "three"),
    ]
    reader = iter(keys("down", "down", "enter")).__next__
    menu = Menu("pick", items, console=Console(), key_reader=reader)
    assert menu.run() == "three"


def test_single_menu_wraps_around_and_backs_out() -> None:
    items = [MenuItem(f"n{index}", f"v{index}") for index in range(3)]
    reader = iter(keys("up", "enter")).__next__
    menu = Menu("pick", items, console=Console(), key_reader=reader)
    assert menu.run() == "v2"  # up from the first row wraps to the last
    reader = iter(keys("escape")).__next__
    menu = Menu("pick", items, console=Console(), key_reader=reader)
    assert menu.run() is None


def test_multi_select_toggles_all_none_and_confirms() -> None:
    items = [
        ToggleItem("a", "a", checked=False),
        ToggleItem("b", "b", checked=False),
        ToggleItem("c", "c", checked=False),
    ]
    reader = iter(keys("space", "down", "down", "space", "enter")).__next__
    picker = MultiSelect("days", items, console=Console(), key_reader=reader)
    assert picker.run() == ["a", "c"]


def test_multi_select_escape_returns_none() -> None:
    items = [ToggleItem("a", "a")]
    reader = iter(keys("q")).__next__
    picker = MultiSelect("days", items, console=Console(), key_reader=reader)
    assert picker.run() is None


def test_empty_menu_is_rejected() -> None:
    with pytest.raises(ValueError):
        Menu("empty", [], console=Console())
    with pytest.raises(ValueError):
        MultiSelect("empty", [], console=Console())


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("SHAKEPIE_20260915T181500Z.spb", "2026-09-15"),
        ("SHAKEPIE_20260915T193000Z_u1789500948834172586.spb", "2026-09-15"),
        ("CASPIAN_20260903T063000Z.recovered.spb", "2026-09-03"),
        ("SHAKEPIE_20260915T193000Z.partial", None),
        ("SHAKEPIE_20260915T193000Z.partial.spb", None),
        ("random-file.txt", None),
        ("not-a-recording.spb", None),
    ],
)
def test_day_of_file_name(name: str, expected: str | None) -> None:
    assert day_of_file_name(name) == expected


def test_partial_files_are_never_finalized() -> None:
    assert is_finalized_recording_name("SHAKEPIE_20260915T181500Z.spb")
    assert not is_finalized_recording_name("SHAKEPIE_20260915T181500Z.partial")
    assert not is_finalized_recording_name("SHAKEPIE_20260915T181500Z.partial.spb")
    assert not is_finalized_recording_name("SHAKEPIE_20260915T181500Z.partial.empty.spb")


def test_human_bytes() -> None:
    assert human_bytes(0) == "0.0 B"
    assert human_bytes(2048) == "2.0 KiB"
    assert human_bytes(5 * 1024**2) == "5.0 MiB"
