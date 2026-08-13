"""Identity-checked permanent recording deletion."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shakepie_archive.catalog import Catalog
from shakepie_archive.downloads import UnsafeFileError, open_catalog_recording


def delete_recording(catalog: Catalog, data_directory: Path, row: Any, username: str) -> None:
    """Delete exactly the catalog-selected file after revalidating its filesystem identity."""
    opened = open_catalog_recording(data_directory, row)
    opened.handle.close()
    candidate = data_directory / str(row["relative_path"])
    try:
        candidate.unlink()
    except OSError as error:
        raise UnsafeFileError(f"permanent deletion failed: {error}") from error
    catalog.mark_deleted(int(row["id"]), username)
