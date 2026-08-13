from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest
from conftest import write_recording
from flask.testing import FlaskClient

from shakepie_archive import recorder_settings
from shakepie_archive.app import create_app
from shakepie_archive.config import RecorderAdminConfig, Settings

RECORDER_CONF = """\
# test recorder configuration
adc.backend=hardware
adc.data_rate_sps=2000
acquisition.samples_per_block=64
acquisition.nominal_group_rate_hz=200
acquisition.max_consecutive_adc_errors=3
storage.rotation_minutes=15
storage.station_id=SHAKEPIE
"""


@pytest.fixture
def admin_settings(settings: Settings, tmp_path: Path) -> Settings:
    recorder_conf = tmp_path / "recorder.conf"
    recorder_conf.write_text(RECORDER_CONF, encoding="utf-8")
    archive_conf = tmp_path / "archive-web.conf"
    archive_conf.write_text(
        "[live]\nsample_rate_hz = 200\n\n[dayplot]\ntarget_sample_rate_hz = 200\n",
        encoding="utf-8",
    )
    return dataclasses.replace(
        settings,
        recorder_admin=RecorderAdminConfig(
            enabled=True,
            recorder_conf_path=recorder_conf,
            archive_conf_path=archive_conf,
            calibrate_request_path=tmp_path / "calibrate.request",
        ),
    )


@pytest.fixture
def admin_client(admin_settings: Settings) -> FlaskClient:
    application = create_app(admin_settings)
    application.config.update(TESTING=True)
    return application.test_client()


def _admin_authenticate(admin_client: FlaskClient) -> str:
    """Open an authenticated session on the admin app's own test client."""
    with admin_client.session_transaction() as session:
        session["authenticated"] = True
        session["username"] = "admin"
        session["csrf_token"] = "test-csrf"
    return "test-csrf"


def test_days_api_requires_authentication(client: FlaskClient) -> None:
    assert client.get("/api/days").status_code == 302


def test_days_api_returns_available_days(
    settings: Settings, catalog, authenticate, client: FlaskClient
) -> None:
    authenticate()
    write_recording(settings.archive.data_directory, "day.spb")
    catalog.reconcile(settings.archive.data_directory)
    response = client.get("/api/days")
    assert response.status_code == 200
    assert response.json == [{"date": "2025-01-01", "recordings": 1}]


def test_settings_page_renders_current_values(
    admin_client: FlaskClient, admin_settings: Settings
) -> None:
    _admin_authenticate(admin_client)
    page = admin_client.get("/settings")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Sample-group rate" in body
    assert 'value="200"' in body
    assert "adc.data_rate_sps" in body


def test_settings_apply_writes_config_restarts_and_audits(
    admin_client: FlaskClient, admin_settings: Settings, monkeypatch
) -> None:
    csrf = _admin_authenticate(admin_client)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        recorder_settings,
        "restart_services",
        lambda services, timeout_seconds=90: (
            calls.append(tuple(services)),
            (list(services), []),
        )[1],
    )
    response = admin_client.post(
        "/settings",
        data={
            "csrf_token": csrf,
            "storage.rotation_minutes": "30",
            "acquisition.nominal_group_rate_hz": "200",
            "adc.data_rate_sps": "2000",
            "acquisition.samples_per_block": "64",
            "acquisition.max_consecutive_adc_errors": "3",
            "storage.station_id": "SHAKEPIE",
        },
    )
    assert response.status_code == 302
    recorder_conf = admin_settings.recorder_admin.recorder_conf_path
    assert "storage.rotation_minutes=30" in recorder_conf.read_text(encoding="utf-8")
    backups = list(recorder_conf.parent.glob("recorder.conf.bak.*"))
    assert len(backups) == 1
    assert calls == [("seisberry-recorder.service",)]
    with sqlite3.connect(admin_settings.archive.catalog_path) as connection:
        audits = connection.execute("SELECT action FROM audit_log").fetchall()
    assert any(row[0] == "recorder_settings_applied" for row in audits)


def test_settings_apply_rejects_unsustainable_rate(
    admin_client: FlaskClient, admin_settings: Settings
) -> None:
    csrf = _admin_authenticate(admin_client)
    response = admin_client.post(
        "/settings",
        data={
            "csrf_token": csrf,
            "acquisition.nominal_group_rate_hz": "1000",
            "adc.data_rate_sps": "2000",
            "acquisition.samples_per_block": "64",
            "acquisition.max_consecutive_adc_errors": "3",
            "storage.rotation_minutes": "15",
            "storage.station_id": "SHAKEPIE",
        },
    )
    assert response.status_code == 302
    recorder_conf = admin_settings.recorder_admin.recorder_conf_path
    assert "acquisition.nominal_group_rate_hz=200" in recorder_conf.read_text(encoding="utf-8")
    page = admin_client.get("/settings")
    assert "six times" in page.get_data(as_text=True)


def test_settings_apply_rejects_bad_csrf(admin_client: FlaskClient) -> None:
    response = admin_client.post("/settings", data={"csrf_token": "wrong"})
    assert response.status_code == 400

def test_settings_calibrate_writes_request_file(
    admin_client: FlaskClient, admin_settings: Settings
) -> None:
    csrf = _admin_authenticate(admin_client)
    response = admin_client.post(
        "/settings/calibrate",
        data={"csrf_token": csrf, "mode": "calibrate"},
    )
    assert response.status_code == 302
    request_file = admin_settings.recorder_admin.calibrate_request_path
    assert request_file.read_text(encoding="utf-8") == "calibrate\n"


def test_settings_calibrate_clear_writes_clear(
    admin_client: FlaskClient, admin_settings: Settings
) -> None:
    csrf = _admin_authenticate(admin_client)
    response = admin_client.post(
        "/settings/calibrate",
        data={"csrf_token": csrf, "mode": "clear"},
    )
    assert response.status_code == 302
    request_file = admin_settings.recorder_admin.calibrate_request_path
    assert request_file.read_text(encoding="utf-8") == "clear\n"


def test_settings_calibrate_rejects_unknown_mode(
    admin_client: FlaskClient, admin_settings: Settings
) -> None:
    csrf = _admin_authenticate(admin_client)
    response = admin_client.post(
        "/settings/calibrate",
        data={"csrf_token": csrf, "mode": "rewrite-everything"},
    )
    assert response.status_code == 400
    assert not admin_settings.recorder_admin.calibrate_request_path.exists()


def test_settings_calibrate_requires_csrf(admin_client: FlaskClient) -> None:
    response = admin_client.post(
        "/settings/calibrate", data={"mode": "calibrate", "csrf_token": "wrong"}
    )
    assert response.status_code == 400
