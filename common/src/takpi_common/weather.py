"""Weather provider – FMI nearest station temp/humidity → chrono + EventBus.

Gates:
  - Location in Finland (bbox 59.5-70.5N, 19.0-31.5E)
  - FMI server reachable (HEAD /wfs?request=getCapabilities, 5 s)

Query:
  - ``fmi::observations::weather::multipointcoverage`` with bbox around current
    location (``±0.5° lat, ±0.7° lon``), ``starttime=-1h``, ``endtime=now``.
  - Lightweight ``requests`` + ``defusedxml`` parse, no ``fmiopendata``/``numpy``.

Publishes :class:`WeatherUpdate` on :class:`EventBus` and drives
``ChronometerClient.gsa72_set_temp_c`` for temperature.
Humidity only goes to event bus (chrono has no humidity cmd).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from takpi_common.bus import EventBus
from takpi_common.location import LocationUpdate, haversine_m

logger = logging.getLogger(__name__)

FINLAND_LAT_MIN = 59.5
FINLAND_LAT_MAX = 70.5
FINLAND_LON_MIN = 19.0
FINLAND_LON_MAX = 31.5

FMI_BASE = "https://opendata.fmi.fi/wfs"
FMI_CAPABILITIES = f"{FMI_BASE}?service=WFS&version=2.0.0&request=getCapabilities"
FMI_STORED_QUERY = "fmi::observations::weather::multipointcoverage"
DEFAULT_INTERVAL_S = 600.0  # 10 min, FMI updates ~10 min


@dataclass(frozen=True)
class WeatherUpdate:
    """Weather from nearest FMI station."""

    temperature_c: float | None  # Air temperature t2m
    humidity_pct: float | None  # Relative humidity rh
    station_name: str | None
    station_lat: float | None
    station_lon: float | None
    distance_m: float | None
    source: str = "fmi"
    timestamp: float = field(default_factory=time.time)


def is_in_finland(lat: float, lon: float) -> bool:
    return (FINLAND_LAT_MIN <= lat <= FINLAND_LAT_MAX) and (
        FINLAND_LON_MIN <= lon <= FINLAND_LON_MAX
    )


async def _is_fmi_reachable(timeout: float = 5.0) -> bool:
    """Check FMI WFS reachability via lightweight HEAD/GET (executor to avoid blocking)."""

    def _check() -> bool:
        try:
            import requests  # type: ignore

            # Use HEAD first, fallback to GET with small timeout
            try:
                r = requests.head(FMI_CAPABILITIES, timeout=timeout)
                if r.ok:
                    return True
            except Exception:
                pass
            r = requests.get(FMI_CAPABILITIES, timeout=timeout, params={})
            return r.ok
        except ImportError:
            logger.warning("requests not installed, cannot check FMI reachability")
            return False
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("FMI reachability check failed: %s", exc)
            return False

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _check)


async def _fetch_fmi_multipoint(
    lat: float, lon: float, timeout: float = 10.0
) -> bytes | None:
    """Fetch multipointcoverage XML for bbox around lat/lon."""

    def _fetch() -> bytes | None:
        try:
            import requests  # type: ignore

            # Bbox: lon ±0.7, lat ±0.5 (~ 60 km)
            lon_min = max(-180.0, lon - 0.7)
            lon_max = min(180.0, lon + 0.7)
            lat_min = max(-90.0, lat - 0.5)
            lat_max = min(90.0, lat + 0.5)
            bbox = f"{lon_min},{lat_min},{lon_max},{lat_max}"
            now = datetime.now(timezone.utc).replace(microsecond=0)
            start = now - timedelta(hours=1)
            url = (
                f"{FMI_BASE}?service=WFS&version=2.0.0&request=getFeature"
                f"&storedquery_id={FMI_STORED_QUERY}"
                f"&bbox={bbox}"
                f"&starttime={start.isoformat().replace('+00:00','Z')}"
                f"&endtime={now.isoformat().replace('+00:00','Z')}"
            )
            logger.debug("FMI query %s", url)
            r = requests.get(url, timeout=timeout)
            if not r.ok:
                logger.warning("FMI query failed %s %s", r.status_code, r.text[:500])
                return None
            return r.content
        except ImportError:
            logger.error("requests not installed, cannot fetch FMI")
            return None
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("FMI fetch error: %s", exc)
            return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch)


def _parse_stations(root: Any, ns: dict[str, str]) -> dict[str, tuple[float, float]]:
    """Parse stations from FMI XML."""
    stations: dict[str, tuple[float, float]] = {}
    for loc in root.findall(".//target:Location", ns):
        name_elem = loc.find(
            'gml:name[@codeSpace="http://xml.fmi.fi/namespace/locationcode/name"]', ns
        )
        if name_elem is None or not name_elem.text:
            continue
        name = name_elem.text.strip()
        href_elem = loc.find("target:representativePoint", ns)
        if href_elem is None:
            continue
        href = href_elem.get("{http://www.w3.org/1999/xlink}href", "")
        point_id = href.lstrip("#")
        if not point_id:
            continue
        for pm in root.findall(".//gml:pointMember", ns):
            pt = pm.find("gml:Point", ns)
            if pt is None:
                continue
            pid = pt.get("{http://www.opengis.net/gml/3.2}id", "")
            if pid != point_id:
                continue
            pos_text = pt.findtext("gml:pos", namespaces=ns)
            if not pos_text:
                continue
            parts = pos_text.strip().split()
            if len(parts) < 2:
                continue
            try:
                lat = float(parts[0])
                lon = float(parts[1])
                stations[name] = (lat, lon)
            except ValueError:
                continue
            break
    return stations


def _find_nearest(
    stations: dict[str, tuple[float, float]], target_lat: float, target_lon: float
) -> tuple[str, float, float, float] | None:
    """Find nearest station."""
    nearest_name: str | None = None
    nearest_dist: float | None = None
    nearest_lat: float | None = None
    nearest_lon: float | None = None
    for name, (slat, slon) in stations.items():
        d = haversine_m(target_lat, target_lon, slat, slon)
        if nearest_dist is None or d < nearest_dist:
            nearest_dist = d
            nearest_name = name
            nearest_lat = slat
            nearest_lon = slon
    if (
        nearest_name is None
        or nearest_lat is None
        or nearest_lon is None
        or nearest_dist is None
    ):
        return None
    return (nearest_name, nearest_lat, nearest_lon, nearest_dist)


def _parse_fmi_positions(
    pos_text: str, tup_text: str, num_fields: int
) -> tuple[list[float], list[float], list[str], int]:
    """Parse positions and tuples."""
    pos_tokens = pos_text.strip().split()
    tup_tokens = tup_text.strip().split()
    try:
        num_positions = len(pos_tokens) // 3
    except Exception:
        return ([], [], [], 0)
    pos_lats: list[float] = []
    pos_lons: list[float] = []
    for i in range(num_positions):
        base = i * 3
        try:
            plat = float(pos_tokens[base])
            plon = float(pos_tokens[base + 1])
            pos_lats.append(plat)
            pos_lons.append(plon)
        except (ValueError, IndexError):
            pos_lats.append(0.0)
            pos_lons.append(0.0)
    return (pos_lats, pos_lons, tup_tokens, num_positions)


def _find_candidate_indices(
    pos_lats: list[float],
    pos_lons: list[float],
    nearest_lat: float,
    nearest_lon: float,
) -> list[int]:
    """Find indices for nearest station."""
    candidate_indices: list[int] = []
    for idx, (plat, plon) in enumerate(zip(pos_lats, pos_lons)):
        if abs(plat - nearest_lat) < 1e-6 and abs(plon - nearest_lon) < 1e-6:
            candidate_indices.append(idx)
    if not candidate_indices:
        for idx, (plat, plon) in enumerate(zip(pos_lats, pos_lons)):
            if haversine_m(plat, plon, nearest_lat, nearest_lon) < 50.0:
                candidate_indices.append(idx)
    return candidate_indices


def _extract_latest(
    candidate_indices: list[int],
    tup_tokens: list[str],
    num_fields: int,
    idx_t2m: int | None,
    idx_rh: int | None,
) -> tuple[float | None, float | None]:
    """Extract latest temp/rh from candidate indices."""

    def _to_float(s: str) -> float | None:
        if s == "NaN":
            return None
        try:
            v = float(s)
            if math.isnan(v):
                return None
            return v
        except ValueError:
            return None

    latest_temp: float | None = None
    latest_rh: float | None = None
    for idx in reversed(candidate_indices):
        tuple_base = idx * num_fields
        if tuple_base + num_fields > len(tup_tokens):
            continue
        row = tup_tokens[tuple_base : tuple_base + num_fields]
        if idx_t2m is not None and latest_temp is None:
            v = _to_float(row[idx_t2m])
            if v is not None:
                latest_temp = v
        if idx_rh is not None and latest_rh is None:
            v = _to_float(row[idx_rh])
            if v is not None:
                latest_rh = v
        if latest_temp is not None and latest_rh is not None:
            break
        if idx_t2m is not None and idx_rh is None and latest_temp is not None:
            break
        if idx_rh is not None and idx_t2m is None and latest_rh is not None:
            break
    return (latest_temp, latest_rh)


def _parse_fmi_xml(
    xml_bytes: bytes, target_lat: float, target_lon: float
) -> WeatherUpdate | None:
    """Parse FMI multipoint XML, find nearest station with latest temp/humidity."""
    try:
        import defusedxml.ElementTree as ET  # type: ignore
    except ImportError:
        logger.error("defusedxml not installed, cannot parse FMI")
        return None

    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("FMI XML parse failed: %s", exc)
        return None

    ns = {
        "wfs": "http://www.opengis.net/wfs/2.0",
        "gml": "http://www.opengis.net/gml/3.2",
        "gmlcov": "http://www.opengis.net/gmlcov/1.0",
        "swe": "http://www.opengis.net/swe/2.0",
        "target": "http://xml.fmi.fi/namespace/om/atmosphericfeatures/1.1",
        "xlink": "http://www.w3.org/1999/xlink",
    }

    stations = _parse_stations(root, ns)
    if not stations:
        logger.warning("FMI: no stations parsed")
        return None

    nearest = _find_nearest(stations, target_lat, target_lon)
    if nearest is None:
        return None
    nearest_name, nearest_lat, nearest_lon, nearest_dist = nearest

    fields = [f.get("name") for f in root.findall(".//swe:field", ns)]
    try:
        idx_t2m = fields.index("t2m")
    except ValueError:
        idx_t2m = None
    try:
        idx_rh = fields.index("rh")
    except ValueError:
        idx_rh = None

    if idx_t2m is None and idx_rh is None:
        logger.warning("FMI: t2m/rh not in fields %s", fields)
        return None

    num_fields = len(fields) if fields else 13

    pos_text = root.findtext(".//gmlcov:positions", namespaces=ns)
    tup_text = root.findtext(".//gml:doubleOrNilReasonTupleList", namespaces=ns)
    if not pos_text or not tup_text:
        logger.warning("FMI: missing positions or tuples")
        return None

    pos_lats, pos_lons, tup_tokens, _ = _parse_fmi_positions(
        pos_text, tup_text, num_fields
    )

    candidate_indices = _find_candidate_indices(
        pos_lats, pos_lons, nearest_lat, nearest_lon
    )
    if not candidate_indices:
        logger.warning("FMI: no positions for nearest station %s", nearest_name)
        return None

    latest_temp, latest_rh = _extract_latest(
        candidate_indices, tup_tokens, num_fields, idx_t2m, idx_rh
    )

    if latest_temp is None and latest_rh is None:
        logger.info(
            "FMI station %s has no valid t2m/rh in last observations", nearest_name
        )
        return WeatherUpdate(
            temperature_c=None,
            humidity_pct=None,
            station_name=nearest_name,
            station_lat=nearest_lat,
            station_lon=nearest_lon,
            distance_m=nearest_dist,
        )

    return WeatherUpdate(
        temperature_c=latest_temp,
        humidity_pct=latest_rh,
        station_name=nearest_name,
        station_lat=nearest_lat,
        station_lon=nearest_lon,
        distance_m=nearest_dist,
        timestamp=time.time(),
    )


class WeatherProvider:
    """Polls FMI for nearest station weather when location in Finland.

    Subscribes to ``LocationUpdate`` on bus to track current position.
    Periodically fetches FMI if ``in_finland`` and FMI reachable.
    Publishes ``WeatherUpdate`` and drives chrono temp.
    """

    def __init__(
        self,
        bus: EventBus,
        chronometer: Any | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        fmi_timeout: float = 10.0,
        reachability_timeout: float = 5.0,
    ) -> None:
        self.bus = bus
        self.chronometer = chronometer
        self.interval_s = interval_s
        self.fmi_timeout = fmi_timeout
        self.reachability_timeout = reachability_timeout
        self._last_location: LocationUpdate | None = None
        self._last_weather: WeatherUpdate | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._unsub_loc: Any | None = None
        self._last_fetch_ts: float = 0.0
        self._fetch_debounce_s: float = 60.0

    async def start(self) -> None:
        """Start provider."""
        if self._running:
            return
        self._running = True
        self._unsub_loc = self.bus.subscribe(LocationUpdate, self._on_location)
        self._task = asyncio.create_task(self._poll_loop(), name="weather-provider")
        logger.info("WeatherProvider started (interval %.0fs)", self.interval_s)

    async def stop(self) -> None:
        """Stop provider."""
        self._running = False
        if self._unsub_loc:
            self._unsub_loc()
            self._unsub_loc = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("WeatherProvider stopped")

    async def __aenter__(self) -> WeatherProvider:
        """Enter async context."""
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit async context."""
        await self.stop()

    async def _on_location(self, evt: LocationUpdate) -> None:
        """Handle LocationUpdate – trigger immediate fetch on config change."""
        prev = self._last_location
        self._last_location = evt
        logger.debug(
            "WeatherProvider got location %.5f,%.5f source=%s",
            evt.latitude,
            evt.longitude,
            evt.source,
        )
        # Only trigger immediate fetch on config.yaml update (per spec), not on every GPS jitter
        if evt.source != "config":
            return
        try:
            should_trigger = False
            if prev is None:
                should_trigger = True
            else:
                # Config file edit – trigger even if small move (user explicitly edited file)
                # Use same 10m jitter threshold as LocationProvider for config, but always trigger if file changed
                dist = haversine_m(
                    prev.latitude, prev.longitude, evt.latitude, evt.longitude
                )
                if dist > 10.0:
                    should_trigger = True
                elif prev.source != "config":
                    # Previous was gpsd, now config → trigger
                    should_trigger = True
                else:
                    # Same source config but location may have been edited to same coordinates? Still trigger if file mtime changed
                    # We treat any new config LocationUpdate as trigger (distance may be 0 if user rewrote same coords)
                    should_trigger = True
            # Debounce: avoid rapid re-fetch if config edited multiple times quickly (<30s)
            if should_trigger and (time.time() - self._last_fetch_ts < 30.0):
                # Allow config trigger to debounce only if very rapid (<10s) and same location
                if (
                    prev is not None
                    and haversine_m(
                        prev.latitude, prev.longitude, evt.latitude, evt.longitude
                    )
                    < 10.0
                    and (time.time() - self._last_fetch_ts < 10.0)
                ):
                    should_trigger = False
            if should_trigger:
                logger.info(
                    "WeatherProvider: config location changed %.5f,%.5f → %.5f,%.5f, triggering immediate fetch",
                    prev.latitude if prev else 0,
                    prev.longitude if prev else 0,
                    evt.latitude,
                    evt.longitude,
                )
                if self._running:
                    asyncio.create_task(self._maybe_fetch_and_publish())
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("WeatherProvider _on_location trigger check failed: %s", exc)

    async def _poll_loop(self) -> None:
        """Poll loop – periodic FMI fetch."""
        # Wait a bit for initial location to arrive
        await asyncio.sleep(2.0)
        while self._running:
            try:
                await self._maybe_fetch_and_publish()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.exception("Weather poll error: %s", exc)
            # Sleep interval, but wake early if location changes to different region?
            # Simple fixed sleep
            try:
                await asyncio.wait_for(
                    self._sleep_cancellable(self.interval_s),
                    timeout=self.interval_s + 0.1,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _sleep_cancellable(self, timeout: float) -> None:
        """Sleep cancellable."""
        await asyncio.sleep(timeout)

    async def _maybe_fetch_and_publish(self) -> None:
        """Fetch FMI if in Finland and publish WeatherUpdate + chrono temp."""
        if self._last_location is None:
            logger.debug("WeatherProvider: no location yet, skip")
            return
        lat = self._last_location.latitude
        lon = self._last_location.longitude
        if not is_in_finland(lat, lon):
            logger.debug(
                "WeatherProvider: location %.5f,%.5f not in Finland, skip", lat, lon
            )
            return
        self._last_fetch_ts = time.time()
        # Check FMI reachability before heavy query
        reachable = await _is_fmi_reachable(timeout=self.reachability_timeout)
        if not reachable:
            logger.info("WeatherProvider: FMI not reachable, skip fetch")
            return
        xml = await _fetch_fmi_multipoint(lat, lon, timeout=self.fmi_timeout)
        if xml is None:
            logger.warning("WeatherProvider: FMI fetch returned None")
            return
        update = _parse_fmi_xml(xml, lat, lon)
        if update is None:
            logger.warning(
                "WeatherProvider: FMI parse returned None for %.5f,%.5f", lat, lon
            )
            return
        self._last_weather = update
        await self.bus.publish(update)
        logger.info(
            "Weather FMI %s %.1f°C %s%% dist=%.0fm",
            update.station_name,
            update.temperature_c if update.temperature_c is not None else float("nan"),
            f"{update.humidity_pct:.0f}" if update.humidity_pct is not None else "NaN",
            update.distance_m or 0,
        )
        # Drive chrono if temp available and chronometer provided
        if update.temperature_c is not None and self.chronometer is not None:
            try:
                # gsa72_set_temp_c expects decicelsius = centidegrees? Actually (decicelsius+50)//100 -> so *100
                decic = int(round(update.temperature_c * 100))
                # Chronometer client is async, call via await
                await self.chronometer.gsa72_set_temp_c(decic)  # type: ignore[attr-defined]
                logger.info(
                    "WeatherProvider: set chrono temp %.1f°C (decic %d)",
                    update.temperature_c,
                    decic,
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("WeatherProvider: failed to set chrono temp: %s", exc)

    async def fetch_now(self, lat: float, lon: float) -> WeatherUpdate | None:
        """Direct fetch for testing – bypass location and publish."""
        if not is_in_finland(lat, lon):
            return None
        xml = await _fetch_fmi_multipoint(lat, lon, timeout=self.fmi_timeout)
        if xml is None:
            return None
        return _parse_fmi_xml(xml, lat, lon)
