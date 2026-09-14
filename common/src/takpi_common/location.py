"""Location provider for takpi – gpsd on localhost with config fallback.

Priority:
  1. Try ``gpsd`` on ``host:port`` (default ``127.0.0.1:2947``). If reachable,
     stream ``TPV`` JSON and publish :class:`LocationUpdate` on :class:`EventBus`
     with 10 m jitter filter and speed-adaptive rate.
  2. If gpsd not reachable, fall back to central YAML ``~/takpi/config.yaml``
     (via :mod:`takpi_common.config_manager`) and poll its ``mtime`` for changes.

Speed-adaptive publish intervals (per spec):
  - ``>10 km/h`` → ``1.0 s``
  - ``6-10 km/h`` → ``5.0 s``
  - ``<6 km/h`` and moving → ``10.0 s``
  - stationary (``<1.8 km/h`` ≈ ``0.5 m/s``) → ``30.0 s``

Jitter filter: only publish if haversine distance from last published
``> JITTER_M`` (default ``10.0 m``) or first publish.

Publishes ``LocationUpdate`` on the bus. Consumers (e.g. ``TakPiApp`` for SA,
``WeatherProvider``) subscribe to it.

GPS time handling:
  - Parses ``TPV`` ``time`` field (ISO8601 UTC) and stores latest GPS UTC.
  - Exposes :meth:`LocationProvider.get_gps_time` for chronometer to use
    GPS-derived UTC + location-derived UTC offset for local hour.

UTC offset handling:
  - Tries ``timezonefinder`` if installed, else if location in Finland → ``Europe/Helsinki``,
    else falls back to longitude-based ``Etc/GMT`` approximation.
  - Helper :func:`utc_to_local` converts UTC to local via zoneinfo/pytz.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from takpi_common.bus import EventBus
from takpi_common.config_manager import (
    ConfigManager,
    get_default_config_path,
    load_yaml_config,
)

logger = logging.getLogger(__name__)

# Constants for speed-adaptive intervals
SPEED_HIGH_KMH = 10.0
SPEED_MID_KMH = 6.0
SPEED_STATIONARY_MPS = 0.5  # ~1.8 km/h
INTERVAL_HIGH_S = 1.0
INTERVAL_MID_S = 5.0
INTERVAL_LOW_S = 10.0
INTERVAL_STATIONARY_S = 30.0
JITTER_M_DEFAULT = 10.0
GPSD_DEFAULT_HOST = "127.0.0.1"
GPSD_DEFAULT_PORT = 2947
GPS_TIME_MAX_AGE_S = 15.0  # GPS time considered fresh for chrono


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in meters between two WGS84 points."""
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def interval_for_speed(speed_mps: float | None) -> float:
    """Map speed (m/s) to publish interval per spec."""
    if speed_mps is None:
        return INTERVAL_LOW_S
    kmh = speed_mps * 3.6
    if kmh > SPEED_HIGH_KMH:
        return INTERVAL_HIGH_S
    if kmh >= SPEED_MID_KMH:
        return INTERVAL_MID_S
    if speed_mps > SPEED_STATIONARY_MPS:
        return INTERVAL_LOW_S
    return INTERVAL_STATIONARY_S


def parse_gps_time(time_str: str | None) -> datetime | None:
    """Parse gpsd TPV time (ISO8601, e.g. 2026-09-14T04:12:43.000Z) → UTC datetime."""
    if not time_str:
        return None
    # gpsd uses ISO8601 with Z
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            dt = datetime.strptime(time_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    # Try fromisoformat fallback (replace Z)
    try:
        if time_str.endswith("Z"):
            time_str = time_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def get_timezone_name(lat: float, lon: float) -> str | None:
    """Best-effort timezone name for lat/lon.

    - Tries ``timezonefinder`` if installed.
    - Else if in Finland bbox → ``Europe/Helsinki``.
    - Else geographic ``Etc/GMT`` approximation.
    """
    # Try timezonefinder first (most accurate worldwide)
    try:
        from timezonefinder import TimezoneFinder  # type: ignore

        tf = TimezoneFinder()  # type: ignore[no-untyped-call]
        tz = tf.timezone_at(lat=lat, lng=lon)  # type: ignore[no-untyped-call]
        if tz:
            return str(tz)
    except ImportError:
        pass
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    # Finland special case (accurate DST via zoneinfo)
    if 59.5 <= lat <= 70.5 and 19.0 <= lon <= 31.5:
        return "Europe/Helsinki"

    # Fallback geographic Etc/GMT (inverted sign)
    try:
        offset = int(round(lon / 15.0))
        if offset == 0:
            return "UTC"
        # Etc/GMT naming: +2 → Etc/GMT-2
        sign = "-" if offset > 0 else "+"
        return f"Etc/GMT{sign}{abs(offset)}"
    except Exception:
        return None


def utc_to_local(utc_dt: datetime, lat: float, lon: float) -> datetime:
    """Convert UTC datetime to local datetime for lat/lon.

    Uses ``zoneinfo`` (with ``tzdata``) or ``pytz`` fallback, else system local.
    """
    # Ensure UTC aware
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    else:
        utc_dt = utc_dt.astimezone(timezone.utc)

    tz_name = get_timezone_name(lat, lon)
    if tz_name:
        # Try zoneinfo first (stdlib, needs tzdata)
        try:
            from zoneinfo import ZoneInfo  # type: ignore

            tz = ZoneInfo(tz_name)
            return utc_dt.astimezone(tz)
        except Exception:
            pass
        # Fallback pytz
        try:
            import pytz  # type: ignore

            tz = pytz.timezone(tz_name)
            return utc_dt.astimezone(tz)
        except Exception:
            pass
        # ZoneInfo with Etc/GMT may have inverted sign but still works
        try:
            from zoneinfo import ZoneInfo  # type: ignore

            tz = ZoneInfo(tz_name)
            return utc_dt.astimezone(tz)
        except Exception:
            pass
    # Fallback system local
    return utc_dt.astimezone()


@dataclass(frozen=True)
class LocationUpdate:
    """Published on :class:`EventBus` when location changes (gpsd or config)."""

    latitude: float
    longitude: float
    altitude: float | None = None
    speed: float | None = None  # m/s
    source: str = "gpsd"  # "gpsd" | "config"
    accuracy: float | None = None  # eph / epx+y or None
    timestamp: float = field(default_factory=time.time)
    gps_time: datetime | None = None  # UTC time from GPS TPV, if available

    def as_tuple(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)


class LocationProvider:
    """Provides location from gpsd or central YAML, with jitter and speed-adaptive rate.

    Example::

        bus = EventBus()
        provider = LocationProvider(bus)
        await provider.start()
        bus.subscribe(LocationUpdate, lambda e: print(e.latitude, e.longitude))
        # ... later
        await provider.stop()
    """

    def __init__(
        self,
        bus: EventBus,
        config_path: Path | str | None = None,
        gpsd_host: str = GPSD_DEFAULT_HOST,
        gpsd_port: int = GPSD_DEFAULT_PORT,
        jitter_m: float = JITTER_M_DEFAULT,
        config_poll_interval: float = 10.0,
        gpsd_probe_timeout: float = 2.0,
        gpsd_reconnect_backoff: float = 5.0,
    ) -> None:
        self.bus = bus
        self.gpsd_host = gpsd_host
        self.gpsd_port = gpsd_port
        self.jitter_m = jitter_m
        self.config_poll_interval = config_poll_interval
        self.gpsd_probe_timeout = gpsd_probe_timeout
        self.gpsd_reconnect_backoff = gpsd_reconnect_backoff
        self.config_manager = ConfigManager(
            path=config_path, poll_interval=config_poll_interval
        )
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._last_published: LocationUpdate | None = None
        self._last_publish_ts: float = 0.0
        self._current_interval: float = INTERVAL_STATIONARY_S
        self._last_gps_time: datetime | None = None
        self._last_gps_time_received: float = 0.0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="location-provider")
        logger.info(
            "LocationProvider started (gpsd %s:%d, jitter %.1fm, config %s)",
            self.gpsd_host,
            self.gpsd_port,
            self.jitter_m,
            self.config_manager.path,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("LocationProvider stopped")

    async def __aenter__(self) -> LocationProvider:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stop()

    def get_last_location(self) -> LocationUpdate | None:
        return self._last_published

    def get_gps_time(self) -> datetime | None:
        """Return last GPS UTC time if fresh (within GPS_TIME_MAX_AGE_S)."""
        if self._last_gps_time is None:
            return None
        # Check age via wall clock vs received time
        if time.time() - self._last_gps_time_received > GPS_TIME_MAX_AGE_S:
            return None
        return self._last_gps_time

    def get_gps_time_and_location(self) -> tuple[datetime, LocationUpdate] | None:
        """Return (gps_time, location) if GPS fix fresh, else None."""
        gps_time = self.get_gps_time()
        loc = self._last_published
        if gps_time is None or loc is None or loc.source != "gpsd":
            return None
        # Also check location freshness
        if time.time() - loc.timestamp > GPS_TIME_MAX_AGE_S:
            return None
        return (gps_time, loc)

    async def _run(self) -> None:
        """Main loop: try gpsd, fall back to config with retry."""
        # Load initial config location so we have something even before gpsd
        initial_cfg = self.config_manager.load()
        if initial_cfg.has_location():
            await self._maybe_publish(
                latitude=initial_cfg.location.latitude or 0.0,
                longitude=initial_cfg.location.longitude or 0.0,
                altitude=initial_cfg.location.altitude,
                speed=None,
                source="config",
                accuracy=None,
                gps_time=None,
                force=True,  # first publish even if jitter not met? but still check
            )

        backoff = 1.0
        max_backoff = 60.0
        while self._running:
            gpsd_available = await self._probe_gpsd()
            if gpsd_available:
                logger.info(
                    "gpsd found at %s:%d, streaming TPV", self.gpsd_host, self.gpsd_port
                )
                backoff = 1.0
                try:
                    await self._gpsd_stream_loop()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.warning("gpsd stream ended: %s", exc)
                if not self._running:
                    break
                logger.info(
                    "gpsd disconnected, retry in %.1fs (fallback to config until then)",
                    backoff,
                )
                # While waiting to reconnect, poll config for fallback updates
                try:
                    await asyncio.wait_for(
                        self._wait_stop_or_timeout(backoff), timeout=backoff + 0.1
                    )
                    # If stop requested during wait, exit
                    if not self._running:
                        break
                except asyncio.TimeoutError:
                    pass
                # On backoff timeout, continue to re-probe gpsd; but also check config during wait?
                # We already waited; now also check config once before re-probe
                await self._poll_config_once()
                backoff = min(backoff * 2.0, max_backoff)
            else:
                logger.info(
                    "gpsd not found at %s:%d, using config %s",
                    self.gpsd_host,
                    self.gpsd_port,
                    self.config_manager.path,
                )
                # Run config-only mode with retry to gpsd periodically
                await self._config_mode_loop()
                # _config_mode_loop only exits on stop or gpsd became available
                backoff = 1.0

    async def _wait_stop_or_timeout(self, timeout: float) -> None:
        """Wait until stop or timeout, but allow config polling in background."""
        # Simple sleep with stop check
        start = time.monotonic()
        while self._running and (time.monotonic() - start) < timeout:
            # Check config for changes while waiting for gpsd reconnect
            await self._poll_config_once()
            await asyncio.sleep(min(2.0, timeout - (time.monotonic() - start)))
            if timeout - (time.monotonic() - start) <= 0:
                break

    async def _probe_gpsd(self) -> bool:
        """Probe if gpsd TCP is reachable (without requiring fix)."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.gpsd_host, self.gpsd_port),
                timeout=self.gpsd_probe_timeout,
            )
            # Try to get banner
            try:
                await asyncio.wait_for(reader.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except (OSError, asyncio.TimeoutError, ConnectionRefusedError):
            return False
        except Exception:  # pylint: disable=broad-exception-caught
            return False

    async def _gpsd_stream_loop(self) -> None:
        """Connect to gpsd and stream TPV, publishing with jitter/speed throttling."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.gpsd_host, self.gpsd_port), timeout=5.0
        )
        try:
            writer.write(b'?WATCH={"enable":true,"json":true}\n')
            await writer.drain()
            # Drain initial banner lines
            # Now loop reading TPV
            while self._running:
                line = await asyncio.wait_for(reader.readline(), timeout=15.0)
                if not line:
                    raise ConnectionError("gpsd closed connection")
                line_s = line.decode("utf-8", errors="ignore").strip()
                if not line_s:
                    continue
                try:
                    data = json.loads(line_s)
                except json.JSONDecodeError:
                    continue
                if data.get("class") != "TPV":
                    continue
                mode = data.get("mode", 0)
                if mode < 2:
                    continue
                lat = data.get("lat")
                lon = data.get("lon")
                if lat is None or lon is None:
                    continue
                try:
                    lat_f = float(lat)
                    lon_f = float(lon)
                except (ValueError, TypeError):
                    continue
                # Validate ranges
                if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
                    continue
                alt = data.get("alt") or data.get("altHAE")
                try:
                    alt_f = float(alt) if alt is not None else None
                except (ValueError, TypeError):
                    alt_f = None
                speed = data.get("speed")
                try:
                    speed_f = float(speed) if speed is not None else None
                except (ValueError, TypeError):
                    speed_f = None
                # Accuracy: eph, epx/epy, epv
                eph = data.get("eph")
                epx = data.get("epx")
                epy = data.get("epy")
                acc: float | None = None
                try:
                    if eph is not None:
                        acc = float(eph)
                    elif epx is not None and epy is not None:
                        acc = math.hypot(float(epx), float(epy))
                except (ValueError, TypeError):
                    acc = None

                # GPS time
                gps_time_str = data.get("time")
                gps_dt = parse_gps_time(gps_time_str) if gps_time_str else None
                if gps_dt is not None:
                    self._last_gps_time = gps_dt
                    self._last_gps_time_received = time.time()

                # Update interval for next publish based on speed
                self._current_interval = interval_for_speed(speed_f)

                # Jitter + interval throttling
                await self._maybe_publish(
                    latitude=lat_f,
                    longitude=lon_f,
                    altitude=alt_f,
                    speed=speed_f,
                    source="gpsd",
                    accuracy=acc,
                    gps_time=gps_dt,
                    force=False,
                )
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _config_mode_loop(self) -> None:
        """Poll config file periodically when gpsd absent, publish on change."""
        # Already published initial if any
        retry_gpsd_after = 60.0
        last_gpsd_retry = time.monotonic()
        while self._running:
            # Check if gpsd became available
            if time.monotonic() - last_gpsd_retry >= retry_gpsd_after:
                if await self._probe_gpsd():
                    logger.info("gpsd became available, switching from config to gpsd")
                    return
                last_gpsd_retry = time.monotonic()
            await self._poll_config_once()
            # Sleep with stop awareness
            try:
                await asyncio.wait_for(
                    self._wait_for_stop(), timeout=self.config_poll_interval
                )
                return  # stop requested
            except asyncio.TimeoutError:
                continue

    async def _wait_for_stop(self) -> None:
        """Wait until stopped; used to make sleep cancellable."""
        while self._running:
            await asyncio.sleep(0.5)

    async def _poll_config_once(self) -> None:
        """Check config file for changes and publish if needed."""
        # Check mtime and reload if changed
        new_cfg = self.config_manager.check_and_reload()
        if new_cfg is None:
            # Also need to handle first load case where config_manager had no mtime but now file exists?
            # If we have no last published but config has location, publish
            if self._last_published is None or self._last_published.source != "config":
                cfg = self.config_manager.config
                if cfg and cfg.has_location():
                    # If we haven't published this location yet, publish
                    # Check if last published was not this exact location
                    lat = cfg.location.latitude or 0.0
                    lon = cfg.location.longitude or 0.0
                    if (
                        self._last_published is None
                        or haversine_m(
                            self._last_published.latitude,
                            self._last_published.longitude,
                            lat,
                            lon,
                        )
                        > 1.0
                    ):
                        await self._maybe_publish(
                            latitude=lat,
                            longitude=lon,
                            altitude=cfg.location.altitude,
                            speed=None,
                            source="config",
                            accuracy=None,
                            gps_time=None,
                            force=True,
                        )
            return
        if new_cfg.has_location():
            lat = new_cfg.location.latitude or 0.0
            lon = new_cfg.location.longitude or 0.0
            await self._maybe_publish(
                latitude=lat,
                longitude=lon,
                altitude=new_cfg.location.altitude,
                speed=None,
                source="config",
                accuracy=None,
                gps_time=None,
                force=True,
            )
            logger.info(
                "Location updated from config %s: %.5f,%.5f", new_cfg.path, lat, lon
            )
        else:
            logger.debug("Config %s has no location, ignoring", new_cfg.path)

    async def _maybe_publish(
        self,
        latitude: float,
        longitude: float,
        altitude: float | None,
        speed: float | None,
        source: str,
        accuracy: float | None,
        gps_time: datetime | None,
        force: bool,
    ) -> None:
        """Apply jitter and interval throttling, then publish LocationUpdate."""
        now = time.monotonic()
        # Interval throttling (except force for initial)
        if not force and self._last_published is not None:
            elapsed = now - self._last_publish_ts
            # Determine effective interval: use current interval for gpsd, else 0 for config ( immediate on change)
            effective_interval = self._current_interval if source == "gpsd" else 0.0
            if elapsed < effective_interval:
                # Not yet time to publish, even if jitter exceeded – skip
                return
        # Jitter filter
        if self._last_published is not None and not force:
            dist = haversine_m(
                self._last_published.latitude,
                self._last_published.longitude,
                latitude,
                longitude,
            )
            if dist < self.jitter_m:
                # Still update speed/interval but don't spam bus
                # However update _current_interval even if not published? Already updated
                return
        # Publish
        evt = LocationUpdate(
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            speed=speed,
            source=source,
            accuracy=accuracy,
            timestamp=time.time(),
            gps_time=gps_time,
        )
        self._last_published = evt
        self._last_publish_ts = now
        if gps_time is not None:
            self._last_gps_time = gps_time
            self._last_gps_time_received = time.time()
        # Update interval for next based on speed (already set in gpsd loop, but also here for config)
        if speed is not None:
            self._current_interval = interval_for_speed(speed)
        elif source == "config":
            self._current_interval = INTERVAL_STATIONARY_S
        await self.bus.publish(evt)
        logger.info(
            "Location %s %.5f,%.5f speed=%.1f m/s interval=%.0fs dist_ok",
            source,
            latitude,
            longitude,
            speed if speed is not None else 0.0,
            self._current_interval,
        )
        # Optionally drive gps_ind LED if configured in hardware (mcp or gpio)
        try:
            cfg = self.config_manager.config
            if cfg is not None:
                # Check mcp gps_ind
                mcp_gps = None
                for a in cfg.hardware.mcp:
                    if a.keyword in ("gps_ind", "gps_fix"):
                        mcp_gps = a
                        break
                if mcp_gps is not None:
                    # gpsd source with good fix → LED on, else off/blink
                    # Use LedCommand for MCP
                    from takpi_common.mcp23017.io import LedCommand  # type: ignore

                    led_state = source == "gpsd" and (
                        accuracy is None or accuracy < 10.0
                    )
                    # For config source, indicate with blink if needed
                    blink = None
                    if source == "config":
                        # Config location has no fix, blink slowly to indicate fallback
                        blink = 1000 if led_state else None
                        led_state = True  # keep on but blink
                    await self.bus.publish(
                        LedCommand(
                            id=mcp_gps.keyword,
                            device_addr=mcp_gps.address,
                            pin=mcp_gps.pin_index,
                            state=led_state,
                            blink_ms=blink,
                        )
                    )
                # For gpio gps_ind, could publish GpioLedCommand in future – currently log only
                for g in cfg.hardware.gpio:
                    if g.keyword in ("gps_ind", "gps_fix"):
                        logger.debug(
                            "GPIO gps_ind on header %d (BCM %d) would be %s",
                            g.header_pin,
                            g.bcm,
                            "on" if source == "gpsd" else "off",
                        )
        except Exception:  # pylint: disable=broad-exception-caught
            pass
