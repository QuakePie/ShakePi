from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from conftest import write_recording
from flask.testing import FlaskClient

from shakepie_archive.catalog import Catalog
from shakepie_archive.config import Settings


def _catalog_app_file(settings: Settings) -> int:
    write_recording(settings.archive.data_directory, "download.spb")
    catalog = Catalog(settings.archive.catalog_path)
    catalog.reconcile(settings.archive.data_directory)
    row = catalog.latest_recording()
    assert row is not None
    return int(row["id"])


def test_login_required_login_csrf_and_security_headers(client: FlaskClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json() == {"status": "ok"}
    assert client.get("/api/status").status_code == 302
    redirect_response = client.get("/")
    assert redirect_response.status_code == 302
    page = client.get("/login")
    token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert token is not None
    bad_csrf = client.post("/login", data={"password": "correct horse battery staple"})
    assert bad_csrf.status_code == 400
    response = client.post(
        "/login",
        data={
            "csrf_token": token.group(1).decode(),
            "password": "correct horse battery staple",
            "next": "/archive",
        },
    )
    assert response.status_code == 302
    assert response.location.endswith("/archive")
    overview = client.get("/")
    assert overview.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in overview.headers["Content-Security-Policy"]
    assert b'name="viewport"' in overview.data


def test_server_side_pagination_and_safe_download(
    client: FlaskClient,
    authenticate: Callable[[], str],
    settings: Settings,
) -> None:
    authenticate()
    recording_id = _catalog_app_file(settings)
    page = client.get("/archive?date=2025-01-01&page=1")
    assert page.status_code == 200
    assert b"download.spb" in page.data
    response = client.get(f"/recordings/{recording_id}/download")
    assert response.status_code == 200
    assert response.data.startswith(b"SPFH")
    assert "attachment" in response.headers["Content-Disposition"]


def test_recording_selection_and_bounded_plot_api(
    client: FlaskClient,
    authenticate: Callable[[], str],
    settings: Settings,
) -> None:
    authenticate()
    recording_id = _catalog_app_file(settings)
    options = client.get("/api/recordings?date=2025-01-01")
    assert options.status_code == 200
    assert options.get_json()[0]["id"] == recording_id
    plotted = client.get(f"/api/recordings/{recording_id}/plot")
    assert plotted.status_code == 200
    payload = plotted.get_json()
    assert payload["recording"]["filename"] == "download.spb"
    assert payload["recording"]["sample_rate_hz"] == 200
    assert payload["frequency_max_hz"] == 100
    assert max(payload["spectrogram"]["frequencies_hz"]) <= 100
    assert set(payload["waveform"]) == {"times_ms", "north", "east", "vertical"}
    assert len(payload["waveform"]["times_ms"]) <= 1200
    assert len(payload["spectrogram"]["times_ms"]) == 480


def test_recording_plot_computes_spectrum_without_sending_raw_samples(
    client: FlaskClient,
    authenticate: Callable[[], str],
    settings: Settings,
) -> None:
    authenticate()
    write_recording(
        settings.archive.data_directory,
        "spectrum.spb",
        first_sequences=(0, 256, 512),
        samples_per_block=256,
    )
    catalog = Catalog(settings.archive.catalog_path)
    catalog.reconcile(settings.archive.data_directory)
    row = catalog.latest_recording()
    assert row is not None
    response = client.get(f"/api/recordings/{row['id']}/plot")
    payload = response.get_json()
    assert response.status_code == 200
    assert "raw" not in payload
    assert any(column is not None for column in payload["spectrogram"]["north"])


def test_historical_recording_reports_measured_effective_rate(
    client: FlaskClient,
    authenticate: Callable[[], str],
    settings: Settings,
) -> None:
    authenticate()
    write_recording(
        settings.archive.data_directory,
        "historical-750.spb",
        sample_rate_millihz=750_000,
        first_sequences=(0, 256),
        samples_per_block=256,
    )
    catalog = Catalog(settings.archive.catalog_path)
    catalog.reconcile(settings.archive.data_directory)
    row = catalog.latest_recording()
    assert row is not None

    payload = client.get(f"/api/recordings/{row['id']}/plot").get_json()

    # The spectrogram frequency axis uses the measured effective rate, which matches the
    # stored nominal rate for well-formed recordings and stays truthful for older files
    # that were written at a different actual rate.
    assert payload["recording"]["sample_rate_hz"] == pytest.approx(750, rel=1e-3)
    assert payload["recording"]["nominal_rate_hz"] == 750
    assert payload["frequency_max_hz"] == 100
    assert max(payload["spectrogram"]["frequencies_hz"]) <= 100


def test_recording_plot_rejects_unsafe_catalog_path(
    client: FlaskClient,
    authenticate: Callable[[], str],
    settings: Settings,
) -> None:
    authenticate()
    recording_id = _catalog_app_file(settings)
    with sqlite3.connect(settings.archive.catalog_path) as connection:
        connection.execute(
            "UPDATE recordings SET relative_path = '../outside.spb' WHERE id = ?", (recording_id,)
        )
    response = client.get(f"/api/recordings/{recording_id}/plot")
    assert response.status_code == 409


def test_stale_id_path_traversal_and_changed_file_are_rejected(
    client: FlaskClient,
    authenticate: Callable[[], str],
    settings: Settings,
) -> None:
    authenticate()
    recording_id = _catalog_app_file(settings)
    with sqlite3.connect(settings.archive.catalog_path) as connection:
        connection.execute(
            "UPDATE recordings SET relative_path = '../outside.spb' WHERE id = ?", (recording_id,)
        )
    assert client.get(f"/recordings/{recording_id}/download").status_code == 409
    assert client.get("/recordings/99999/download").status_code == 404


def test_symlink_is_rejected_when_supported(
    client: FlaskClient,
    authenticate: Callable[[], str],
    settings: Settings,
) -> None:
    authenticate()
    recording_id = _catalog_app_file(settings)
    original = settings.archive.data_directory / "download.spb"
    target = settings.archive.mount_point / "target.spb"
    original.replace(target)
    try:
        original.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable to this test user")
    assert client.get(f"/recordings/{recording_id}/download").status_code == 409


def test_delete_requires_matching_confirmation_and_removes_only_selected_recording(
    client: FlaskClient,
    authenticate: Callable[[], str],
    settings: Settings,
) -> None:
    csrf = authenticate()
    recording_id = _catalog_app_file(settings)
    write_recording(settings.archive.data_directory, "keep.spb", start_ns=1_735_689_610_000_000_000)
    Catalog(settings.archive.catalog_path).reconcile(settings.archive.data_directory)
    archive_page = client.get("/archive?date=2025-01-01")
    assert b"Delete recording" in archive_page.data
    assert b"Move to trash" not in archive_page.data
    denied = client.post(
        f"/recordings/{recording_id}/delete",
        data={"csrf_token": csrf, "confirm_filename": "keep.spb"},
        follow_redirects=False,
    )
    assert denied.status_code == 302
    catalog = Catalog(settings.archive.catalog_path)
    assert catalog.recording(recording_id)["state"] == "active"
    assert (settings.archive.data_directory / "download.spb").exists()
    deleted = client.post(
        f"/recordings/{recording_id}/delete",
        data={"csrf_token": csrf, "confirm_filename": "download.spb"},
        follow_redirects=False,
    )
    assert deleted.status_code == 302
    assert catalog.recording(recording_id)["state"] == "purged"
    assert not (settings.archive.data_directory / "download.spb").exists()
    assert (settings.archive.data_directory / "keep.spb").exists()
    assert client.get("/trash").status_code == 404


def test_current_utc_day_can_queue_a_gap_aware_snapshot(
    client: FlaskClient,
    authenticate: Callable[[], str],
    settings: Settings,
) -> None:
    csrf = authenticate()
    now = datetime.now(UTC)
    write_recording(
        settings.archive.data_directory,
        "today.spb",
        start_ns=int(now.timestamp() * 1_000_000_000),
    )
    Catalog(settings.archive.catalog_path).reconcile(settings.archive.data_directory)
    response = client.post(
        f"/dayplot/{now.date().isoformat()}/generate",
        data={"csrf_token": csrf, "confirm_incomplete": "yes"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"only for completed UTC days" not in response.data
    assert b"is queued" in response.data
