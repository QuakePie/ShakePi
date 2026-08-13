"""Race-aware opening of catalog-resolved finalized recordings."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, BinaryIO


class UnsafeFileError(RuntimeError):
    """Raised when a catalog entry no longer resolves to the indexed regular file."""


@dataclass(slots=True)
class OpenedRecording:
    handle: BinaryIO
    filename: str
    size_bytes: int
    mtime: float


def _safe_leaf(relative_path: str) -> str:
    path = PurePath(relative_path)
    if path.is_absolute() or len(path.parts) != 1 or path.name != relative_path:
        raise UnsafeFileError("catalog path is outside the recording directory")
    if not relative_path.endswith(".spb"):
        raise UnsafeFileError("catalog entry is not a finalized recording")
    return relative_path


def open_catalog_recording(root: Path, row: Any) -> OpenedRecording:
    leaf = _safe_leaf(str(row["relative_path"]))
    candidate = root / leaf
    try:
        before = candidate.lstat()
    except OSError as error:
        raise UnsafeFileError("recording is no longer available") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise UnsafeFileError("recording is not a regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise UnsafeFileError("recording could not be opened safely") from error
    try:
        after = os.fstat(descriptor)
        expected = (
            int(row["device"]),
            int(row["inode"]),
            int(row["size_bytes"]),
            int(row["mtime_ns"]),
        )
        actual = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if actual != expected or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise UnsafeFileError("recording changed after it was cataloged")
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
        return OpenedRecording(handle, str(row["filename"]), after.st_size, after.st_mtime)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
