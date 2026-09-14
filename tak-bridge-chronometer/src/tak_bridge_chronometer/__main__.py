"""
Main entry for tak-bridge-chronometer.

Long-running asyncio service that keeps the Flight Illusion GSA-72 Chronometer
in sync with the Raspberry Pi system time. Sets local hour and UTC H:M:S
every hour (default 3600 s) via the ``chronometer`` library which ports
``ArduIllusion.cpp:161`` Chrono functions. Also provides location (gpsd →
``~/takpi/config.yaml`` fallback, 10 m jitter, speed-adaptive 1/5/10/30 s) and
weather (Finland + FMI nearest station temp→chrono + humidity→EventBus) via
``takpi_common``.

Usage:
  poetry run start
  python -m tak_bridge_chronometer

Env vars (via .env or environment):
  CHRONO_PORT      Serial device, e.g. /dev/serial0 (GPIO UART, default /dev/serial0) – Pi GPIO 14/15
  CHRONO_BAUDRATE  38400 (default, FI fixed)
  UPDATE_INTERVAL  Seconds between time syncs (default 3600)
  HEALTH_FILE      Path to health flag file (default /tmp/tak-chronometer-healthy)
  DEBUG            1 for DEBUG logging
  CHRONO_LIGHT     Optional DL000000 light value (e.g. 192=0b11000000, 0.24) for backlight
  CHRONO_BUZZER    Optional buzzer value (0 off, 1 on; discovered cmd 10 2026-09-10)
  CHRONO_SEMICOLON Optional semicolon blink alt char (0 steady, 32=0x20 space blink, 1=U, 2=L; cmd 13)
  TAKPI_CONFIG     Path to central YAML config (default ~/takpi/config.yaml, fallback /etc/takpi/config.yaml)
  GPSD_HOST        gpsd host (default 127.0.0.1)
  GPSD_PORT        gpsd port (default 2947)
  LOCATION_JITTER_M  Jitter filter meters (default 10)
  WEATHER_INTERVAL Seconds between FMIqueries (default 600)
  WEATHER_ENABLED  1 to force enable, 0 to disable (auto: enabled if location in Finland+internet)
  TAK_HOST         TAK server host for Location→SA CotStream (optional, e.g. tak.example.com)
  TAK_PORT         TAK server port (default 8089)
  TAK_CERT/TAK_KEY/TAK_CA  mTLS PEMs for CotStream (optional)
  TAK_CALLSIGN     Callsign for SA reports (default TAKPI-CHRONO)
  TAK_TEAM/TAK_ROLE Team/role for SA (default Cyan/Team Member)
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import signal
from typing import NoReturn

from dotenv import load_dotenv

from .chronometer import ChronometerClient, GSA72_ID

# Optional takpi_common providers (location/weather/cot) – may be absent in minimal installs
try:
    from takpi_common.bus import EventBus
    from takpi_common.location import LocationProvider
    from takpi_common.location_cot import LocationCotBridge
    from takpi_common.weather import WeatherProvider

    _HAS_TAKPI_COMMON = True
except ImportError:
    EventBus = None  # type: ignore[assignment,misc]
    LocationProvider = None  # type: ignore[assignment,misc]
    LocationCotBridge = None  # type: ignore[assignment,misc]
    WeatherProvider = None  # type: ignore[assignment,misc]
    _HAS_TAKPI_COMMON = False

logger = logging.getLogger(__name__)

HEALTH_FILE: str = os.getenv("HEALTH_FILE", "/tmp/tak-chronometer-healthy")
HEALTH_MAX_ERRORS: int = int(os.getenv("HEALTH_MAX_ERRORS", "3"))


def _report_health(ok: bool) -> None:
    """Health file helper – same pattern as tak-feeder-traffic-fin-roads:feeder.py:23."""
    if ok:
        try:
            os.unlink(HEALTH_FILE)
        except FileNotFoundError:
            pass
    else:
        with open(HEALTH_FILE, "w", encoding="utf-8") as f:
            f.write("unhealthy")


async def sync_once(
    client: ChronometerClient, location_provider: object | None = None
) -> None:
    """
    Set Chronometer local hour and UTC from Pi system time, or GPS if available.

    Uses:
      - ``gsa72_setLocal`` with local hour (``ArduIllusion.cpp:185``)
      - ``gsa72_setUTC(h,m,s)`` with UTC H:M:S (``ArduIllusion.cpp:168``)

    If ``location_provider`` has a fresh GPS fix (``gpsd`` within 15 s),
    GPS UTC time and GPS-derived local timezone (via ``utc_to_local``) are used
    for higher accuracy and correct UTC offset when moving across zones.
    Otherwise Pi system time (NTP) is used; if provider has any location
    (config or stale GPS) we still derive local hour from that location's
    timezone (``Europe/Helsinki`` for Finland) instead of plain ``astimezone()``.
    """
    local_hour: int
    utc_h: int
    utc_m: int
    utc_s: int

    # Try GPS time + location-derived offset first
    gps_used = False
    if location_provider is not None:
        try:
            # Prefer fresh GPS time + location
            get_gps = getattr(location_provider, "get_gps_time_and_location", None)
            if callable(get_gps):
                gps_tuple = get_gps()  # type: ignore[operator]
                if gps_tuple is not None:
                    gps_utc, gps_loc = gps_tuple  # type: ignore[misc]
                    # Ensure gps_utc is aware UTC
                    if gps_utc.tzinfo is None:
                        gps_utc = gps_utc.replace(tzinfo=datetime.timezone.utc)
                    # Derive local via timezone helper
                    try:
                        from takpi_common.location import utc_to_local  # type: ignore

                        local_dt = utc_to_local(
                            gps_utc, gps_loc.latitude, gps_loc.longitude
                        )
                        local_hour = local_dt.hour
                    except Exception:
                        local_hour = gps_utc.astimezone().hour
                    utc_h = gps_utc.hour
                    utc_m = gps_utc.minute
                    utc_s = gps_utc.second
                    logger.info(
                        "Syncing Chronometer from GPS – local %02d:00 (GPS %.5f,%.5f) UTC %02d:%02d:%02d",
                        local_hour,
                        gps_loc.latitude,
                        gps_loc.longitude,
                        utc_h,
                        utc_m,
                        utc_s,
                    )
                    gps_used = True
                else:
                    # No fresh GPS time, but we may still have a location for offset
                    get_loc = getattr(location_provider, "get_last_location", None)
                    if callable(get_loc):
                        last_loc = get_loc()  # type: ignore[operator]
                        if last_loc is not None:
                            now_utc = datetime.datetime.now(datetime.timezone.utc)
                            try:
                                from takpi_common.location import utc_to_local  # type: ignore

                                local_dt = utc_to_local(
                                    now_utc, last_loc.latitude, last_loc.longitude
                                )
                                local_hour = local_dt.hour
                            except Exception:
                                local_hour = now_utc.astimezone().hour
                            utc_h = now_utc.hour
                            utc_m = now_utc.minute
                            utc_s = now_utc.second
                            logger.info(
                                "Syncing Chronometer – local %02d:00 (derived from %.5f,%.5f) UTC %02d:%02d:%02d",
                                local_hour,
                                last_loc.latitude,
                                last_loc.longitude,
                                utc_h,
                                utc_m,
                                utc_s,
                            )
                            gps_used = True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("GPS time handling failed, fallback to system time: %s", exc)

    if not gps_used:
        # Fallback to Pi system time
        now_local = datetime.datetime.now().astimezone()
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        local_hour = now_local.hour
        utc_h = now_utc.hour
        utc_m = now_utc.minute
        utc_s = now_utc.second
        logger.info(
            "Syncing Chronometer – local %02d:00 (local %s) UTC %02d:%02d:%02d",
            local_hour,
            now_local.tzname() or "local",
            utc_h,
            utc_m,
            utc_s,
        )

    # GSA-72 expects local hour only (0-23)
    await client.gsa72_set_local(local_hour)
    # Small gap between commands – FI gauges need 2 ms per byte already handled,
    # but inter-command gap helps avoid overrun if Pi is fast
    await asyncio.sleep(0.05)
    await client.gsa72_set_utc_hms(utc_h, utc_m, utc_s)

    # Optional persistent settings (buzzer/light/semicolon) – re-assert each sync
    # so gauge recovers after power cycle. Values via env, int(...,0) allows 0x20/0b..
    light = os.getenv("CHRONO_LIGHT")
    if light is not None and light != "":
        try:
            await client.set_light(GSA72_ID, int(light, 0))
            await asyncio.sleep(0.05)
        except ValueError:
            logger.warning("Invalid CHRONO_LIGHT %r", light)
    buzzer = os.getenv("CHRONO_BUZZER")
    if buzzer is not None and buzzer != "":
        try:
            await client.gsa72_set_buzzer(int(buzzer, 0))
            await asyncio.sleep(0.05)
        except ValueError:
            logger.warning("Invalid CHRONO_BUZZER %r", buzzer)
    semicolon = os.getenv("CHRONO_SEMICOLON")
    if semicolon is not None and semicolon != "":
        try:
            await client.gsa72_set_semicolon_blink(int(semicolon, 0))
            await asyncio.sleep(0.05)
        except ValueError:
            logger.warning("Invalid CHRONO_SEMICOLON %r", semicolon)


async def sync_loop(
    client: ChronometerClient,
    interval: int,
    location_provider: object | None = None,
) -> NoReturn:
    """Run :func:`sync_once` every ``interval`` seconds forever."""
    consecutive_errors: int = 0
    while True:
        try:
            await sync_once(client, location_provider)
            consecutive_errors = 0
            _report_health(True)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            consecutive_errors += 1
            logger.error(
                "Chronometer sync failed (%d/%d): %s",
                consecutive_errors,
                HEALTH_MAX_ERRORS,
                exc,
            )
            if consecutive_errors >= HEALTH_MAX_ERRORS:
                _report_health(False)

        # Sleep exactly interval; for hourly wall-clock alignment set interval=3600
        # and optionally sleep until next hour on first iteration if desired.
        await asyncio.sleep(float(interval))


async def main() -> None:
    """Entry point – connect and start hourly sync loop with reconnect + location/weather."""
    load_dotenv()

    port: str = os.getenv("CHRONO_PORT", os.getenv("SERIAL_PORT", "/dev/serial0"))
    baudrate: int = int(os.getenv("CHRONO_BAUDRATE", os.getenv("BAUDRATE", "38400")))
    interval: int = int(os.getenv("UPDATE_INTERVAL", "3600"))
    debug: str = os.getenv("DEBUG", "0")

    # Allow HEALTH_FILE override via env after import-time default
    global HEALTH_FILE
    HEALTH_FILE = os.getenv("HEALTH_FILE", HEALTH_FILE)

    log_level: int = logging.DEBUG if debug == "1" else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # GPIO UART is canonical – warn if using non-GPIO port (e.g., USB adapter left over)
    if port not in ("/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0", "/dev/serial1"):
        if (
            port.startswith("/dev/ttyUSB")
            or port.startswith("/dev/ttyACM")
            or "by-id" in port
        ):
            logger.warning(
                "Using %s – this bridge is configured for Pi GPIO UART (/dev/serial0 on GPIO14/15), not USB. See wiring in README_chronometer.md",
                port,
            )

    logger.info(
        "tak-bridge-chronometer starting – port=%s baud=%d interval=%ds",
        port,
        baudrate,
        interval,
    )

    # Graceful shutdown handling
    stop_event: asyncio.Event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        logger.info("Signal received, shutting down…")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows / non-main thread – ignore
            pass

    # ------------------------------------------------------------------
    # Setup EventBus + Location/Weather/COT providers (optional, via takpi_common)
    # ------------------------------------------------------------------
    bus = None
    location_provider = None
    weather_provider = None
    cot_bus = None
    location_cot_bridge = None
    cot_stream = None

    if _HAS_TAKPI_COMMON and EventBus is not None:
        bus = EventBus()
        # Location provider (gpsd → config fallback)
        try:
            takpi_config_path = os.getenv("TAKPI_CONFIG")
            gpsd_host = os.getenv("GPSD_HOST", "127.0.0.1")
            gpsd_port = int(os.getenv("GPSD_PORT", "2947"))
            jitter_m = float(os.getenv("LOCATION_JITTER_M", "10"))
            location_provider = LocationProvider(
                bus,
                config_path=takpi_config_path,
                gpsd_host=gpsd_host,
                gpsd_port=gpsd_port,
                jitter_m=jitter_m,
                config_poll_interval=10.0,
            )
            await location_provider.start()
            logger.info(
                "LocationProvider started (gpsd %s:%d, jitter %.1fm)",
                gpsd_host,
                gpsd_port,
                jitter_m,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("LocationProvider failed to start: %s", exc)
            location_provider = None

        # Weather provider – needs chronometer client later, but start now with placeholder
        # Will be re-bound to real client after ChronometerClient is created
        weather_enabled = os.getenv("WEATHER_ENABLED", "auto").strip().lower()
        if weather_enabled not in ("0", "false", "no", "off"):
            try:
                weather_interval = float(os.getenv("WEATHER_INTERVAL", "600"))
                # Create with no chronometer yet; will set after client connect
                weather_provider = WeatherProvider(
                    bus, chronometer=None, interval_s=weather_interval
                )
                await weather_provider.start()
                logger.info(
                    "WeatherProvider started (interval %.0fs)", weather_interval
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("WeatherProvider failed to start: %s", exc)
                weather_provider = None
        else:
            logger.info("WeatherProvider disabled via WEATHER_ENABLED=0")

        # COT bridge for Location → SA (if TAK_HOST configured)
        tak_host = os.getenv("TAK_HOST") or os.getenv("TAKPI_TAK_HOST")
        if tak_host and location_provider is not None:
            try:
                from takpi_common.cot_bus import CotBus  # type: ignore[import-not-found]

                tak_port = int(
                    os.getenv("TAK_PORT", os.getenv("TAKPI_TAK_PORT", "8089"))
                )
                tak_cert = os.getenv("TAK_CERT") or os.getenv("TAKPI_CERT")
                tak_key = os.getenv("TAK_KEY") or os.getenv("TAKPI_KEY")
                tak_ca = os.getenv("TAK_CA") or os.getenv("TAKPI_CA")
                tak_callsign = os.getenv(
                    "TAK_CALLSIGN", os.getenv("TAKPI_CALLSIGN", "TAKPI-CHRONO")
                )
                tak_team = os.getenv("TAK_TEAM", os.getenv("TAKPI_TEAM", "Cyan"))
                tak_role = os.getenv("TAK_ROLE", os.getenv("TAKPI_ROLE", "Team Member"))
                try:
                    from takstream import CotStream  # type: ignore[import-not-found]

                    logger.info(
                        "Connecting CotStream to %s:%d for Location SA",
                        tak_host,
                        tak_port,
                    )
                    cot_stream = await CotStream.connect(
                        tak_host,
                        tak_port,
                        cert=tak_cert,
                        key=tak_key,
                        ca=tak_ca,
                        callsign=tak_callsign,
                        team=tak_team,
                        role=tak_role,
                    )
                    cot_bus = CotBus(bus, cot_stream)
                    await cot_bus.start()
                    location_cot_bridge = LocationCotBridge(
                        bus, callsign=tak_callsign, team=tak_team, role=tak_role
                    )
                    await location_cot_bridge.start()
                    logger.info(
                        "Location→COT bridge started (callsign=%s)", tak_callsign
                    )
                except ImportError as exc:
                    logger.warning("takstream not available, COT SA disabled: %s", exc)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.warning(
                        "CotStream connect failed %s:%d: %s", tak_host, tak_port, exc
                    )
                    cot_bus = None
                    location_cot_bridge = None
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("Location→COT setup failed: %s", exc)
    else:
        if not _HAS_TAKPI_COMMON:
            logger.info(
                "takpi_common not available – running time sync only (no location/weather)"
            )

    backoff: float = 1.0
    max_backoff: float = 60.0

    try:
        while not stop_event.is_set():
            client = ChronometerClient(port=port, baudrate=baudrate)
            # Bind weather provider to this client so temp can be set
            if weather_provider is not None:
                weather_provider.chronometer = client  # type: ignore[attr-defined]
            try:
                await client.connect()
                logger.info("Connected to Chronometer, starting hourly sync")
                backoff = 1.0
                _report_health(True)

                # Run sync loop until cancelled or connection lost
                sync_task: asyncio.Task[NoReturn] = asyncio.create_task(
                    sync_loop(client, interval, location_provider)
                )
                # Wait for either stop_event or task completion (task never completes normally)
                done, _ = await asyncio.wait(
                    [sync_task, asyncio.create_task(stop_event.wait())],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # If stop requested, cancel sync task
                for t in done:
                    if t.get_name() == sync_task.get_name():
                        # sync_loop only exits on exception – will be handled below
                        task_exc = t.exception()
                        if task_exc:
                            raise task_exc  # pylint: disable=raising-bad-type
                    else:
                        # stop_event set
                        sync_task.cancel()
                        try:
                            await sync_task
                        except asyncio.CancelledError:
                            pass
                        await client.disconnect()
                        logger.info("Shutdown complete")
                        return

            except asyncio.CancelledError:
                logger.info("Cancelled, disconnecting…")
                try:
                    await client.disconnect()
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.error(
                    "Chronometer connection/sync error: %s – retry in %.1fs",
                    exc,
                    backoff,
                )
                _report_health(False)
                try:
                    await client.disconnect()
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
                # Wait with backoff or until stop requested
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                    # stop requested during backoff
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2.0, max_backoff)

        logger.info("Exiting main")
    finally:
        # Cleanup providers
        if location_cot_bridge is not None:
            try:
                await location_cot_bridge.stop()
            except Exception:
                pass
        if cot_bus is not None:
            try:
                await cot_bus.stop()
            except Exception:
                pass
        if cot_stream is not None:
            try:
                await cot_stream.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        if weather_provider is not None:
            try:
                await weather_provider.stop()
            except Exception:
                pass
        if location_provider is not None:
            try:
                await location_provider.stop()
            except Exception:
                pass


def run() -> None:
    """Sync entry for poetry ``start`` script (``poetry run start``)."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
