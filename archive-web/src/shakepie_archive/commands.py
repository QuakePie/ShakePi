"""Administrative command-line entry points."""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from werkzeug.security import generate_password_hash

from shakepie_archive.catalog import Catalog
from shakepie_archive.config import load_settings, prepare_filesystem


def reconcile_main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile the ShakePie archive catalog")
    parser.add_argument("--config", type=Path)
    arguments = parser.parse_args()
    settings = load_settings(arguments.config)
    prepare_filesystem(settings)
    catalog = Catalog(settings.archive.catalog_path)
    catalog.initialize()
    result = catalog.reconcile(settings.archive.data_directory)
    print(
        "indexed={indexed} invalid={invalid} skipped={skipped} missing={missing}".format(**result)
    )


def password_main() -> None:
    first = getpass.getpass("New archive password: ")
    second = getpass.getpass("Repeat password: ")
    if len(first) < 8:
        raise SystemExit("password must contain at least 8 characters")
    if len(first) < 12:
        print("warning: passwords shorter than 12 characters are easier to guess")
    if first != second:
        raise SystemExit("passwords do not match")
    print(generate_password_hash(first, method="scrypt"))
