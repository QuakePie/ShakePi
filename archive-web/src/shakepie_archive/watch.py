"""Earthquake watch points: poll USGS, ISC, and/or CENC for quakes near saved locations.

Each watch point has a name, a coordinate, a radius, and a catalog source (USGS, ISC,
CENC — the China Earthquake Networks Center — or a combination). On every check the
service queries the selected catalogs for the window since the last check, keeps the
quakes inside the radius, and pushes a Bark summary when something new is found.
Points, the check interval, and the found earthquakes persist in a JSON file under
the recorder's state directory (/var/lib/shakepie), so edits from the web interface
survive restarts.

CENC publishes a rolling JSON feed of the year's events (no query parameters, no
official API), so the whole feed is fetched and filtered to the check window here.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

QUAKES_CAP_PER_POINT = 400
DEFAULT_LOOKBACK_HOURS = 24
LOOKBACK_BUFFER_MINUTES = 30
MIN_CHECK_GAP_SECONDS = 300
CENC_DEFAULT_BASE = "https://www.ceic.ac.cn"
CENC_TIMEZONE = timezone(timedelta(hours=8))  # CENC lists times in Beijing time


# ---------------------------------------------------------------------------
# data model + validation


@dataclass(frozen=True, slots=True)
class WatchPoint:
    name: str
    latitude: float
    longitude: float
    radius_km: int
    sources: str  # "usgs" | "isc" | "both"
    min_magnitude: float
    created_at: str


@dataclass(frozen=True, slots=True)
class QuakeHit:
    source: str
    source_id: str
    time_ns: int
    magnitude: float | None
    depth_km: float | None
    latitude: float
    longitude: float
    place: str
    distance_km: float


NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
SOURCE_LISTS = {
    "usgs": ["usgs"],
    "isc": ["isc"],
    "cenc": ["cenc"],
    "both": ["usgs", "isc"],
    "usgs_cenc": ["usgs", "cenc"],
    "all": ["usgs", "isc", "cenc"],
}


def validate_point_fields(
    name: str, latitude: str, longitude: str, radius_km: str,
    min_magnitude: str, sources: str,
) -> tuple[list[str], WatchPoint | None]:
    """Validate raw form fields; returns (errors, parsed point or None)."""
    errors: list[str] = []
    if not NAME_PATTERN.match(name):
        errors.append("name must be 1-40 characters: letters, digits, '-' and '_'")
    try:
        lat = float(latitude)
        if not -90 <= lat <= 90:
            raise ValueError
    except ValueError:
        errors.append("latitude must be a number between -90 and 90")
    try:
        lon = float(longitude)
        if not -180 <= lon <= 180:
            raise ValueError
    except ValueError:
        errors.append("longitude must be a number between -180 and 180")
    try:
        radius = int(radius_km)
        if not 1 <= radius <= 2000:
            raise ValueError
    except ValueError:
        errors.append("radius must be between 1 and 2000 km")
    try:
        minmag = round(float(min_magnitude), 1)
        if not 0 <= minmag <= 10:
            raise ValueError
    except ValueError:
        errors.append("minimum magnitude must be between 0 and 10")
    if sources not in SOURCE_LISTS:
        errors.append("source must be usgs, isc, cenc, both, usgs_cenc, or all")
    if errors:
        return errors, None
    point = WatchPoint(
        name=name,
        latitude=lat,
        longitude=lon,
        radius_km=radius,
        sources=sources,
        min_magnitude=minmag,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    return [], point


def sources_for(choice: str) -> list[str]:
    return SOURCE_LISTS.get(choice, ["usgs"])


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometres."""
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# catalog queries


def usgs_query_url(
    base: str, point: WatchPoint, start_dt: datetime, end_dt: datetime
) -> str:
    return (
        f"{base.rstrip('/')}/fdsnws/event/1/query?format=geojson&orderby=time&limit=200"
        f"&starttime={start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&endtime={end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&latitude={point.latitude}&longitude={point.longitude}"
        f"&maxradiuskm={point.radius_km}&minmagnitude={point.min_magnitude}"
    )


def isc_query_url(
    base: str, point: WatchPoint, start_dt: datetime, end_dt: datetime
) -> str:
    return (
        f"{base.rstrip('/')}/fdsnws/event/1/query?format=text"
        f"&starttime={start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&endtime={end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&latitude={point.latitude}&longitude={point.longitude}"
        f"&maxradiuskm={point.radius_km}&minmagnitude={point.min_magnitude}"
    )


def cenc_feed_url(base: str) -> str:
    """The CENC catalog site serves its rolling event list as a static JSON file."""
    return f"{base.rstrip('/')}/data/data.json"


def _optional_float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _coerce_float(value: Any) -> float | None:
    """Accept numbers or numeric strings; CENC occasionally quotes its fields."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_usgs_geojson(text: str) -> list[dict[str, Any]]:
    """Flatten a USGS geojson feature list into raw quake dicts (time in ns)."""
    payload = json.loads(text)
    quakes: list[dict[str, Any]] = []
    for feature in payload.get("features", []):
        properties = feature.get("properties", {})
        geometry = feature.get("geometry", {}).get("coordinates", [])
        event_time = properties.get("time")
        if not isinstance(event_time, (int, float)) or len(geometry) < 2:
            continue
        quakes.append(
            {
                "source_id": str(feature.get("id", "")),
                "time_ns": int(event_time) * 1_000_000,
                "magnitude": properties.get("mag"),
                "depth_km": geometry[2] if len(geometry) > 2 else None,
                "latitude": float(geometry[1]),
                "longitude": float(geometry[0]),
                "place": str(properties.get("place") or ""),
            }
        )
    return quakes


def parse_isc_text(text: str) -> list[dict[str, Any]]:
    """Parse the ISC FDSN pipe-delimited text format using its header mapping."""
    quakes: list[dict[str, Any]] = []
    columns: dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            columns = {
                part.strip().lstrip("#").strip(): index
                for index, part in enumerate(line.split("|"))
            }
            continue
        parts = [part.strip() for part in line.split("|")]
        if not columns or len(parts) < len(columns):
            continue
        needed = ("EventID", "Time", "Magnitude", "Latitude", "Longitude")
        if any(name not in columns for name in needed):
            continue

        def value(name: str, parts: list[str] = parts, columns: dict[str, int] = columns) -> str:
            extracted: str = parts[columns[name]]
            return extracted

        try:
            time_ns = int(
                datetime.fromisoformat(value("Time").replace("Z", "")).replace(
                    tzinfo=UTC
                ).timestamp()
                * 1e9
            )
            latitude = float(value("Latitude"))
            longitude = float(value("Longitude"))
        except ValueError:
            continue
        magnitude = _optional_float(value("Magnitude"))
        depth = _optional_float(value("Depth/km"))
        quakes.append(
            {
                "source_id": value("EventID"),
                "time_ns": time_ns,
                "magnitude": magnitude,
                "depth_km": depth,
                "latitude": latitude,
                "longitude": longitude,
                "place": value("Event Region").strip() if "Event Region" in columns else "",
            }
        )
    return quakes


def parse_cenc_json(text: str) -> list[dict[str, Any]]:
    """Parse the CENC JSON feed into raw quake dicts (time in ns, UTC-based).

    Rows with unusable times or coordinates are skipped so one malformed entry
    cannot drop the rest of the feed.
    """
    payload = json.loads(text)
    if not isinstance(payload, list):
        return []
    quakes: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            naive = datetime.strptime(str(item["time"]), "%Y-%m-%d %H:%M:%S")
            latitude = float(item["latitude"])
            longitude = float(item["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        magnitude = _coerce_float(item.get("magnitude"))
        depth = _coerce_float(item.get("depth"))
        quakes.append(
            {
                "source_id": str(item.get("id", "")),
                "time_ns": int(naive.replace(tzinfo=CENC_TIMEZONE).timestamp() * 1e9),
                "magnitude": magnitude,
                "depth_km": depth,
                "latitude": latitude,
                "longitude": longitude,
                "place": str(item.get("location") or ""),
            }
        )
    return quakes


def fetch_text(url: str, timeout: float) -> str:
    with urlopen(Request(url, headers={"User-Agent": "ShakePie/1.0"}), timeout=timeout) as reply:
        body: bytes = reply.read()
        return body.decode("utf-8", errors="replace")


def query_source(
    source: str,
    point: WatchPoint,
    start_dt: datetime,
    end_dt: datetime,
    *,
    usgs_base: str,
    isc_base: str,
    cenc_base: str = CENC_DEFAULT_BASE,
    fetch: Callable[[str], str],
) -> list[QuakeHit]:
    """Query one catalog and return the quakes inside the point's radius."""
    start_ns = int(start_dt.timestamp() * 1e9)
    end_ns = int(end_dt.timestamp() * 1e9)
    if source == "usgs":
        url = usgs_query_url(usgs_base, point, start_dt, end_dt)
        raw = parse_usgs_geojson(fetch(url))
    elif source == "isc":
        url = isc_query_url(isc_base, point, start_dt, end_dt)
        raw = parse_isc_text(fetch(url))
    elif source == "cenc":
        # The feed has no server-side filtering: fetch the rolling list, then keep
        # only the quakes inside this check's window.
        raw = [
            item
            for item in parse_cenc_json(fetch(cenc_feed_url(cenc_base)))
            if start_ns <= item["time_ns"] <= end_ns
        ]
    else:
        return []
    hits: list[QuakeHit] = []
    for item in raw:
        hits.append(
            QuakeHit(
                source=source,
                source_id=item["source_id"],
                time_ns=item["time_ns"],
                magnitude=item["magnitude"],
                depth_km=item["depth_km"],
                latitude=item["latitude"],
                longitude=item["longitude"],
                place=item["place"],
                distance_km=haversine_km(
                    point.latitude, point.longitude, item["latitude"], item["longitude"]
                ),
            )
        )
    return hits


# ---------------------------------------------------------------------------
# persistent store (points + found quakes + last-check status)


class WatchStore:
    """Thread-safe JSON persistence for watch points and their found earthquakes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _empty(self) -> dict[str, Any]:
        return {"interval_hours": 1, "points": [], "quakes": [], "last_check": {}}

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o770, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
        temporary.replace(self.path)

    # -- points -----------------------------------------------------------------
    def interval_hours(self) -> int:
        with self._lock:
            value = self._load().get("interval_hours", 1)
        return value if isinstance(value, int) and value >= 1 else 1

    def set_interval_hours(self, hours: int) -> None:
        with self._lock:
            data = self._load()
            data["interval_hours"] = max(1, min(168, hours))
            self._save(data)

    def points(self) -> list[WatchPoint]:
        with self._lock:
            raw = self._load().get("points", [])
        return [
            WatchPoint(
                name=str(item["name"]),
                latitude=float(item["latitude"]),
                longitude=float(item["longitude"]),
                radius_km=int(item["radius_km"]),
                sources=str(item["sources"]),
                min_magnitude=float(item.get("min_magnitude", 0.0)),
                created_at=str(item.get("created_at", "")),
            )
            for item in raw
            if isinstance(item, dict) and "name" in item
        ]

    def get_point(self, name: str) -> WatchPoint | None:
        return next((point for point in self.points() if point.name == name), None)

    def upsert_point(self, point: WatchPoint) -> None:
        with self._lock:
            data = self._load()
            data["points"] = [
                item for item in data.get("points", []) if item.get("name") != point.name
            ]
            data["points"].append(asdict(point))
            self._save(data)

    def delete_point(self, name: str) -> bool:
        with self._lock:
            data = self._load()
            remaining = [item for item in data.get("points", []) if item.get("name") != name]
            if len(remaining) == len(data.get("points", [])):
                return False
            data["points"] = remaining
            data["quakes"] = [q for q in data.get("quakes", []) if q.get("point") != name]
            data.get("last_check", {}).pop(name, None)
            self._save(data)
            return True

    # -- quakes -----------------------------------------------------------------
    def quakes(self, point_name: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            quakes = list(self._load().get("quakes", []))
        if point_name:
            quakes = [q for q in quakes if q.get("point") == point_name]
        return sorted(quakes, key=lambda q: q.get("time_ns", 0), reverse=True)

    def add_quakes(self, point_name: str, hits: list[QuakeHit], now: datetime) -> int:
        """Add quakes for a point that are not already stored; returns the new count."""
        now_iso = now.isoformat(timespec="seconds")
        added = 0
        with self._lock:
            data = self._load()
            existing = {q.get("key") for q in data.get("quakes", [])}
            for hit in sorted(hits, key=lambda h: h.time_ns):
                key = f"{point_name}|{hit.source}:{hit.source_id}"
                if key in existing:
                    continue
                existing.add(key)
                data.setdefault("quakes", []).append(
                    {
                        "key": key,
                        "point": point_name,
                        "source": hit.source,
                        "source_id": hit.source_id,
                        "time_ns": hit.time_ns,
                        "magnitude": hit.magnitude,
                        "depth_km": hit.depth_km,
                        "latitude": hit.latitude,
                        "longitude": hit.longitude,
                        "place": hit.place,
                        "distance_km": round(hit.distance_km, 1),
                        "found_at": now_iso,
                    }
                )
                added += 1
            point_quakes = [q for q in data["quakes"] if q.get("point") == point_name]
            point_quakes.sort(key=lambda q: q.get("time_ns", 0), reverse=True)
            dropped = {
                q["key"] for q in point_quakes[QUAKES_CAP_PER_POINT:]
            }
            if dropped:
                data["quakes"] = [q for q in data["quakes"] if q["key"] not in dropped]
            self._save(data)
        return added

    # -- last check -------------------------------------------------------------
    def set_last_check(
        self, name: str, at: datetime, status: str, error: str, new_count: int
    ) -> None:
        with self._lock:
            data = self._load()
            data.setdefault("last_check", {})[name] = {
                "at": at.isoformat(timespec="seconds"),
                "status": status,
                "error": error[:300],
                "new": new_count,
            }
            self._save(data)

    def last_check(self, name: str) -> dict[str, Any]:
        with self._lock:
            value = self._load().get("last_check", {}).get(name, {})
        return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# checking


def query_window(last_check_at: str, now: datetime) -> tuple[datetime, datetime]:
    """Query from the previous check (minus a small overlap), capped at 24 h."""
    end_dt = now
    if last_check_at:
        try:
            start_dt = datetime.fromisoformat(last_check_at) - timedelta(
                minutes=LOOKBACK_BUFFER_MINUTES
            )
        except ValueError:
            start_dt = now - timedelta(hours=DEFAULT_LOOKBACK_HOURS)
    else:
        start_dt = now - timedelta(hours=DEFAULT_LOOKBACK_HOURS)
    earliest = now - timedelta(hours=DEFAULT_LOOKBACK_HOURS)
    return max(start_dt, earliest), end_dt


def check_point(
    store: WatchStore,
    point: WatchPoint,
    now: datetime,
    notifier: Any | None,
    *,
    usgs_base: str,
    isc_base: str,
    cenc_base: str = CENC_DEFAULT_BASE,
    timeout: float,
    fetch: Callable[[str], str] | None = None,
) -> tuple[int, str]:
    """Query the point's sources, store new quakes, and push a summary. Returns (new, error)."""
    # Read the previous check time before marking this one, or the query window
    # would shrink to the 30-minute overlap instead of "since the last check".
    previous = store.last_check(point.name).get("at")
    previous_at = previous if isinstance(previous, str) else ""
    store.set_last_check(point.name, now, "checking", "", 0)
    start_dt, end_dt = query_window(previous_at, now)
    new_hits: list[QuakeHit] = []
    errors: list[str] = []
    for source in sources_for(point.sources):
        try:
            hits = query_source(
                source,
                point,
                start_dt,
                end_dt,
                usgs_base=usgs_base,
                isc_base=isc_base,
                cenc_base=cenc_base,
                fetch=fetch or (lambda url: fetch_text(url, timeout)),
            )
        except Exception as source_error:
            errors.append(f"{source}: {source_error}")
            continue
        new_hits.extend(
            hit
            for hit in hits
            if hit.distance_km <= point.radius_km
            and (hit.magnitude is None or hit.magnitude >= point.min_magnitude)
        )
    added = store.add_quakes(point.name, new_hits, now)
    error = "; ".join(errors)
    status = "ok" if not error else "error"
    store.set_last_check(point.name, now, status, error, added)
    if added and notifier is not None:
        largest = sorted(
            new_hits, key=lambda hit: -(hit.magnitude if hit.magnitude is not None else -99)
        )[:3]
        details = "; ".join(
            f"M{hit.magnitude if hit.magnitude is not None else '?'} "
            f"{hit.distance_km:.0f} km"
            for hit in largest
        )
        if added > len(largest):
            details += f" (+{added - len(largest)} more)"
        notifier.send_now(
            f"ShakePie: {added} new quake{'s' if added != 1 else ''} near {point.name}",
            details,
        )
    return added, error


class WatchService:
    """Background thread that checks all watch points on the configured interval."""

    def __init__(
        self,
        store: WatchStore,
        notifier: Any | None,
        *,
        usgs_base: str,
        isc_base: str,
        cenc_base: str = CENC_DEFAULT_BASE,
        timeout: float,
        first_check_delay: float = 30.0,
    ) -> None:
        self._store = store
        self._notifier = notifier
        self._usgs_base = usgs_base
        self._isc_base = isc_base
        self._cenc_base = cenc_base
        self._timeout = timeout
        self._first_check_delay = first_check_delay
        self._stop = threading.Event()
        self._check_now = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_result = {"at": "", "status": "idle", "error": "", "new": 0}

    def request_check(self) -> None:
        self._check_now.set()

    def stop(self) -> None:
        self._stop.set()
        self._check_now.set()

    def last_result(self) -> dict[str, Any]:
        return dict(self._last_result)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="shakepie-watch", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        self._stop.wait(self._first_check_delay)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                new_total = 0
                for point in self._store.points():
                    if self._stop.is_set():
                        break
                    added, _error = check_point(
                        self._store,
                        point,
                        datetime.now(UTC),
                        self._notifier,
                        usgs_base=self._usgs_base,
                        isc_base=self._isc_base,
                        cenc_base=self._cenc_base,
                        timeout=self._timeout,
                    )
                    new_total += added
                self._last_result = {
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "status": "ok",
                    "error": "",
                    "new": new_total,
                }
            except Exception as error:
                self._last_result = {
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "status": "error",
                    "error": str(error)[:300],
                    "new": 0,
                }
                print(f"watch: check failed: {error}", flush=True)
            interval_seconds = max(
                MIN_CHECK_GAP_SECONDS, self._store.interval_hours() * 3600
            )
            while (
                not self._stop.is_set()
                and not self._check_now.is_set()
                and time.monotonic() - started < interval_seconds
            ):
                self._stop.wait(2.0)
            self._check_now.clear()
