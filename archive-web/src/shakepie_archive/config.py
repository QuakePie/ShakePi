"""Strict configuration loading for the archive services."""

from __future__ import annotations

import configparser
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from shakepie_archive.constants import NOMINAL_SAMPLE_RATE_HZ, NYQUIST_FREQUENCY_HZ


class ConfigurationError(ValueError):
    """Raised when a deployment configuration is unsafe or incomplete."""


@dataclass(frozen=True, slots=True)
class WebConfig:
    bind_address: str = "127.0.0.1"
    port: int = 8050
    workers: int = 1
    threads: int = 2
    status_refresh_seconds: int = 15


@dataclass(frozen=True, slots=True)
class ArchiveConfig:
    mount_point: Path = Path("/mnt/shakepie-data")
    data_directory: Path = Path("/mnt/shakepie-data/recordings")
    catalog_path: Path = Path("/mnt/shakepie-data/index/archive.sqlite")
    reconcile_minutes: int = 15
    require_mount: bool = True


@dataclass(frozen=True, slots=True)
class DayplotConfig:
    output_directory: Path = Path("/mnt/shakepie-data/dayplots")
    temporary_directory: Path = Path("/mnt/shakepie-data/dayplots/tmp")
    output_format: str = "png"
    interval_minutes: int = 60
    width_pixels: int = 1600
    target_sample_rate_hz: int = NOMINAL_SAMPLE_RATE_HZ
    maximum_concurrent_jobs: int = 1
    minimum_free_gib: int = 5
    memory_limit_mib: int = 1024
    worker_nice: int = 15
    io_scheduling_class: str = "idle"
    idle_sleep_seconds: int = 10
    auto_generate_days: bool = True
    auto_check_seconds: int = 600


@dataclass(frozen=True, slots=True)
class LiveConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    history_seconds: int = 30
    refresh_milliseconds: int = 1000
    idle_disconnect_seconds: int = 30
    frequency_max_hz: int = NYQUIST_FREQUENCY_HZ
    sample_rate_hz: int = NOMINAL_SAMPLE_RATE_HZ


@dataclass(frozen=True, slots=True)
class RecordedPlotConfig:
    waveform_bins: int = 1200
    spectrogram_columns: int = 480
    fft_size: int = 256
    frequency_max_hz: int = NYQUIST_FREQUENCY_HZ


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    session_cookie_secure: bool = False
    session_lifetime_minutes: int = 60
    secret_key: str = ""
    password_hash: str = ""
    login_attempts: int = 5
    login_window_seconds: int = 300


@dataclass(frozen=True, slots=True)
class RecorderAdminConfig:
    """Paths used by the administrative recorder-settings page."""

    enabled: bool = True
    recorder_conf_path: Path = Path("/etc/shakepie/recorder.conf")
    archive_conf_path: Path = Path("/etc/shakepie/archive-web.conf")
    calibrate_request_path: Path = Path("/var/lib/shakepie/calibrate.request")


@dataclass(frozen=True, slots=True)
class EventDetectorConfig:
    """STA/LTA trigger bounds; validated further by shakepie_archive.detector.

    ``minimum_components`` is the N-of-M coincidence gate: 1 keeps the legacy
    vertical-only trigger, 2 or 3 require that many components to trigger
    simultaneously before an event is stored.
    """

    enabled: bool = True
    sta_seconds: float = 1.0
    lta_seconds: float = 30.0
    threshold: float = 4.0
    off_seconds: float = 5.0
    cooldown_seconds: float = 30.0
    maximum_events: int = 1000
    minimum_components: int = 1


@dataclass(frozen=True, slots=True)
class WatchConfig:
    """USGS/ISC/CENC watch points; points and interval live in the state JSON file."""

    enabled: bool = True
    state_path: Path = Path("/var/lib/shakepie/watch.json")
    timeout_seconds: int = 20
    usgs_base: str = "https://earthquake.usgs.gov"
    isc_base: str = "https://isc.ac.uk"
    cenc_base: str = "https://www.ceic.ac.cn"


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    """Bark push notifications for detected events (iOS app, api.day.app).

    The device key is a secret and arrives through the environment as
    ``SHAKEPIE_NOTIFY_BARK_KEY``; without it, notifications stay disabled.
    """

    enabled: bool = True
    bark_server: str = "https://api.day.app"
    rate_limit_seconds: int = 600
    daily_summary: bool = True
    summary_hour: int = 0  # hour in Beijing time (0 = midnight)
    bark_key: str = ""


@dataclass(frozen=True, slots=True)
class Settings:
    web: WebConfig
    archive: ArchiveConfig
    dayplot: DayplotConfig
    security: SecurityConfig
    live: LiveConfig = LiveConfig()
    recorded_plot: RecordedPlotConfig = RecordedPlotConfig()
    recorder_admin: RecorderAdminConfig = RecorderAdminConfig()
    event_detector: EventDetectorConfig = EventDetectorConfig()
    notify: NotifyConfig = NotifyConfig()
    watch: WatchConfig = WatchConfig()
    health_file: Path = Path("/run/shakepie/health.json")
    source_path: Path | None = None


def _integer(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = parser.getint(section, option, fallback=default)
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{section}.{option} must be between {minimum} and {maximum}")
    return value


def _absolute_path(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ConfigurationError(f"{label} must be an absolute path")
    return path


def load_settings(path: Path | None = None) -> Settings:
    """Load an INI file and protected secrets supplied through the environment."""
    configured = path or Path(
        os.environ.get("SHAKEPIE_ARCHIVE_CONFIG", "/etc/shakepie/archive-web.conf")
    )
    parser = configparser.ConfigParser(interpolation=None)
    if configured.exists():
        try:
            with configured.open("r", encoding="utf-8") as handle:
                parser.read_file(handle)
        except (OSError, configparser.Error) as error:
            raise ConfigurationError(f"cannot read {configured}: {error}") from error
    elif path is not None or "SHAKEPIE_ARCHIVE_CONFIG" in os.environ:
        raise ConfigurationError(f"configuration file does not exist: {configured}")

    web = WebConfig(
        bind_address=parser.get("web", "bind_address", fallback="127.0.0.1"),
        port=_integer(parser, "web", "port", 8050, 1, 65_535),
        workers=_integer(parser, "web", "workers", 1, 1, 1),
        threads=_integer(parser, "web", "threads", 2, 1, 8),
        status_refresh_seconds=_integer(parser, "web", "status_refresh_seconds", 15, 15, 3600),
    )
    archive = ArchiveConfig(
        mount_point=_absolute_path(
            parser.get("archive", "mount_point", fallback="/mnt/shakepie-data"),
            "archive.mount_point",
        ),
        data_directory=_absolute_path(
            parser.get("archive", "data_directory", fallback="/mnt/shakepie-data/recordings"),
            "archive.data_directory",
        ),
        catalog_path=_absolute_path(
            parser.get(
                "archive", "catalog_path", fallback="/mnt/shakepie-data/index/archive.sqlite"
            ),
            "archive.catalog_path",
        ),
        reconcile_minutes=_integer(parser, "archive", "reconcile_minutes", 15, 1, 1440),
        require_mount=parser.getboolean("archive", "require_mount", fallback=True),
    )
    dayplot = DayplotConfig(
        output_directory=_absolute_path(
            parser.get("dayplot", "output_directory", fallback="/mnt/shakepie-data/dayplots"),
            "dayplot.output_directory",
        ),
        temporary_directory=_absolute_path(
            parser.get(
                "dayplot",
                "temporary_directory",
                fallback="/mnt/shakepie-data/dayplots/tmp",
            ),
            "dayplot.temporary_directory",
        ),
        output_format=parser.get("dayplot", "output_format", fallback="png").lower(),
        interval_minutes=_integer(parser, "dayplot", "interval_minutes", 60, 1, 1440),
        width_pixels=_integer(parser, "dayplot", "width_pixels", 1600, 640, 4096),
        target_sample_rate_hz=_integer(
            parser,
            "dayplot",
            "target_sample_rate_hz",
            NOMINAL_SAMPLE_RATE_HZ,
            25,
            2000,
        ),
        maximum_concurrent_jobs=_integer(parser, "dayplot", "maximum_concurrent_jobs", 1, 1, 1),
        minimum_free_gib=_integer(parser, "dayplot", "minimum_free_gib", 5, 1, 1024),
        memory_limit_mib=_integer(parser, "dayplot", "memory_limit_mib", 1024, 512, 8192),
        worker_nice=_integer(parser, "dayplot", "worker_nice", 15, 0, 19),
        io_scheduling_class=parser.get("dayplot", "io_scheduling_class", fallback="idle").lower(),
        idle_sleep_seconds=_integer(parser, "dayplot", "idle_sleep_seconds", 10, 2, 300),
        auto_generate_days=parser.getboolean("dayplot", "auto_generate_days", fallback=True),
        auto_check_seconds=_integer(parser, "dayplot", "auto_check_seconds", 600, 60, 3600),
    )
    live = LiveConfig(
        enabled=parser.getboolean("live", "enabled", fallback=True),
        host=parser.get("live", "host", fallback="127.0.0.1"),
        port=_integer(parser, "live", "port", 8765, 1, 65_535),
        history_seconds=_integer(parser, "live", "history_seconds", 30, 5, 120),
        refresh_milliseconds=_integer(
            parser, "live", "refresh_milliseconds", 1000, 500, 5000
        ),
        idle_disconnect_seconds=_integer(
            parser, "live", "idle_disconnect_seconds", 30, 10, 600
        ),
        frequency_max_hz=_integer(
            parser, "live", "frequency_max_hz", NYQUIST_FREQUENCY_HZ, 1, NYQUIST_FREQUENCY_HZ
        ),
        sample_rate_hz=_integer(
            parser,
            "live",
            "sample_rate_hz",
            NOMINAL_SAMPLE_RATE_HZ,
            25,
            2000,
        ),
    )
    recorded_plot = RecordedPlotConfig(
        waveform_bins=_integer(parser, "recorded_plot", "waveform_bins", 1200, 200, 4000),
        spectrogram_columns=_integer(
            parser, "recorded_plot", "spectrogram_columns", 480, 100, 1200
        ),
        fft_size=_integer(parser, "recorded_plot", "fft_size", 256, 64, 4096),
        frequency_max_hz=_integer(
            parser,
            "recorded_plot",
            "frequency_max_hz",
            NYQUIST_FREQUENCY_HZ,
            1,
            NYQUIST_FREQUENCY_HZ,
        ),
    )
    security = SecurityConfig(
        session_cookie_secure=parser.getboolean(
            "security", "session_cookie_secure", fallback=False
        ),
        session_lifetime_minutes=_integer(
            parser, "security", "session_lifetime_minutes", 60, 5, 1440
        ),
        secret_key=os.environ.get("SHAKEPIE_ARCHIVE_SECRET_KEY", ""),
        password_hash=os.environ.get("SHAKEPIE_ARCHIVE_PASSWORD_HASH", ""),
        login_attempts=_integer(parser, "security", "login_attempts", 5, 1, 50),
        login_window_seconds=_integer(parser, "security", "login_window_seconds", 300, 30, 3600),
    )
    recorder_admin = RecorderAdminConfig(
        enabled=parser.getboolean("recorder_admin", "enabled", fallback=True),
        recorder_conf_path=_absolute_path(
            parser.get(
                "recorder_admin", "recorder_conf_path", fallback="/etc/shakepie/recorder.conf"
            ),
            "recorder_admin.recorder_conf_path",
        ),
        archive_conf_path=_absolute_path(
            parser.get(
                "recorder_admin", "archive_conf_path", fallback="/etc/shakepie/archive-web.conf"
            ),
            "recorder_admin.archive_conf_path",
        ),
        calibrate_request_path=_absolute_path(
            parser.get(
                "recorder_admin",
                "calibrate_request_path",
                fallback="/var/lib/shakepie/calibrate.request",
            ),
            "recorder_admin.calibrate_request_path",
        ),
    )
    event_detector = EventDetectorConfig(
        enabled=parser.getboolean("detector", "enabled", fallback=True),
        sta_seconds=parser.getfloat("detector", "sta_seconds", fallback=1.0),
        lta_seconds=parser.getfloat("detector", "lta_seconds", fallback=30.0),
        threshold=parser.getfloat("detector", "threshold", fallback=4.0),
        off_seconds=parser.getfloat("detector", "off_seconds", fallback=5.0),
        cooldown_seconds=parser.getfloat("detector", "cooldown_seconds", fallback=30.0),
        maximum_events=parser.getint("detector", "maximum_events", fallback=1000),
        minimum_components=_integer(parser, "detector", "minimum_components", 1, 1, 3),
    )
    watch = WatchConfig(
        enabled=parser.getboolean("watch", "enabled", fallback=True),
        state_path=_absolute_path(
            parser.get("watch", "state_path", fallback="/var/lib/shakepie/watch.json"),
            "watch.state_path",
        ),
        timeout_seconds=_integer(parser, "watch", "timeout_seconds", 20, 5, 120),
        usgs_base=parser.get("watch", "usgs_base", fallback="https://earthquake.usgs.gov"),
        isc_base=parser.get("watch", "isc_base", fallback="https://isc.ac.uk"),
        cenc_base=parser.get("watch", "cenc_base", fallback="https://www.ceic.ac.cn"),
    )
    notify = NotifyConfig(
        enabled=parser.getboolean("notify", "enabled", fallback=True),
        bark_server=parser.get("notify", "bark_server", fallback="https://api.day.app"),
        rate_limit_seconds=_integer(parser, "notify", "rate_limit_seconds", 600, 0, 86_400),
        daily_summary=parser.getboolean("notify", "daily_summary", fallback=True),
        summary_hour=_integer(parser, "notify", "summary_hour", 0, 0, 23),
        bark_key=os.environ.get("SHAKEPIE_NOTIFY_BARK_KEY", ""),
    )
    health_file = _absolute_path(
        parser.get("health", "status_file", fallback="/run/shakepie/health.json"),
        "health.status_file",
    )
    settings = Settings(
        web=web,
        archive=archive,
        dayplot=dayplot,
        security=security,
        live=live,
        recorded_plot=recorded_plot,
        recorder_admin=recorder_admin,
        event_detector=event_detector,
        notify=notify,
        watch=watch,
        health_file=health_file,
        source_path=configured,
    )
    validate_values(settings)
    return settings


def validate_values(settings: Settings) -> None:
    if settings.web.bind_address not in {"127.0.0.1", "0.0.0.0", "::1"}:
        raise ConfigurationError("web.bind_address must be loopback or 0.0.0.0")
    if settings.dayplot.output_format not in {"png", "jpg"}:
        raise ConfigurationError("dayplot.output_format must be png or jpg")
    if settings.dayplot.io_scheduling_class != "idle":
        raise ConfigurationError("dayplot.io_scheduling_class must be idle")
    if settings.live.host not in {"127.0.0.1", "::1", "localhost"}:
        raise ConfigurationError("live.host must be a loopback address")
    if settings.recorded_plot.fft_size & (settings.recorded_plot.fft_size - 1):
        raise ConfigurationError("recorded_plot.fft_size must be a power of two")
    if len(settings.security.secret_key) < 32:
        raise ConfigurationError("SHAKEPIE_ARCHIVE_SECRET_KEY must contain at least 32 characters")
    if not settings.security.password_hash.startswith(("scrypt:", "pbkdf2:")):
        raise ConfigurationError(
            "SHAKEPIE_ARCHIVE_PASSWORD_HASH must be a Werkzeug scrypt or PBKDF2 hash"
        )


def prepare_filesystem(settings: Settings) -> None:
    """Validate the HDD boundary and create only service-owned metadata directories."""
    mount = settings.archive.mount_point
    data = settings.archive.data_directory
    if not mount.is_dir():
        raise ConfigurationError(f"archive mount is unavailable: {mount}")
    if settings.archive.require_mount and os.name == "posix" and not os.path.ismount(mount):
        raise ConfigurationError(f"archive mount is not an active mount point: {mount}")
    if not data.is_dir() or data.is_symlink():
        raise ConfigurationError(f"recording directory is missing or unsafe: {data}")
    mount_resolved = mount.resolve(strict=True)
    try:
        data.resolve(strict=True).relative_to(mount_resolved)
    except ValueError as error:
        raise ConfigurationError("recording directory is outside the archive mount") from error

    managed_directories = (
        settings.archive.catalog_path.parent,
        settings.dayplot.output_directory,
        settings.dayplot.temporary_directory,
    )
    for directory in managed_directories:
        try:
            directory.resolve(strict=False).relative_to(mount_resolved)
        except ValueError as error:
            raise ConfigurationError(
                f"managed path is outside the archive mount: {directory}"
            ) from error
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        information = directory.lstat()
        if stat.S_ISLNK(information.st_mode) or not stat.S_ISDIR(information.st_mode):
            raise ConfigurationError(f"managed path is not a real directory: {directory}")
        if information.st_dev != data.stat().st_dev:
            raise ConfigurationError(
                f"managed path is not on the recording filesystem: {directory}"
            )
