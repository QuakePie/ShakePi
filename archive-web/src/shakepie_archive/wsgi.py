"""Gunicorn application entry point."""

from shakepie_archive.app import create_app

app = create_app()
