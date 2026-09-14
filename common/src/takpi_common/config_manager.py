"""Central YAML config manager for takpi.

Resolves ``~/takpi/config.yaml`` (default) or ``TAKPI_CONFIG`` override,
loads ``location.latitude`` / ``location.longitude`` and watches for changes.

Default path per spec: ``$HOME/takpi/config.yaml`` (``~/takpi/config.yaml``).
Fallback: ``/etc/takpi/config.yaml`` for system installs.

Example ``config.yaml``:

.. code-block:: yaml

    location:
      latitude: 60.1699
      longitude: 24.9384
      # altitude: 10  # optional, meters

    # future: other sections (tak, chronometer, etc.)

Env override still works for legacy: ``TAK_LAT``/``TAK_LON`` or ``LAT``/``LON``
are checked if YAML has no location.

File is polled for ``mtime`` changes by :class:`ConfigManager` or by callers
like :class:`takpi_common.location.LocationProvider` when gpsd is absent.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / "takpi" / "config.yaml"
FALLBACK_CONFIG_PATH = Path("/etc/takpi/config.yaml")
ENV_CONFIG_VAR = "TAKPI_CONFIG"


@dataclass(frozen=True)
class LocationConfig:
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None

    def is_valid(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def as_tuple(self) -> tuple[float, float] | None:
        if self.latitude is None or self.longitude is None:
            return None
        return (self.latitude, self.longitude)


@dataclass
class TakpiConfig:
    location: LocationConfig = field(default_factory=LocationConfig)
    raw: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None
    mtime: float | None = None

    def has_location(self) -> bool:
        return self.location.is_valid()


def get_default_config_path() -> Path:
    """Resolve central config path: ``TAKPI_CONFIG`` > ``~/takpi/config.yaml`` > ``/etc/takpi/config.yaml``."""
    env_path = os.getenv(ENV_CONFIG_VAR)
    if env_path:
        p = Path(env_path).expanduser()
        # Return env path even if it does not exist – caller will handle FileNotFound and try fallback if desired.
        # But if env is set explicitly, respect it strictly.
        return p
    # Prefer home path even if not yet created; fallback only if home missing?
    # Check existence: if default exists, use it; else if fallback exists, use fallback.
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    if FALLBACK_CONFIG_PATH.exists():
        return FALLBACK_CONFIG_PATH
    return DEFAULT_CONFIG_PATH


def _get_env_location() -> LocationConfig | None:
    """Legacy env fallback: TAK_LAT/TAK_LON or LAT/LON or TAKPI_LAT/LON."""
    # Order: TAKPI_LAT, TAK_LAT, LAT
    lat_str = (
        os.getenv("TAKPI_LAT")
        or os.getenv("TAK_LAT")
        or os.getenv("LAT")
        or os.getenv("TAKPI_LATITUDE")
    )
    lon_str = (
        os.getenv("TAKPI_LON")
        or os.getenv("TAK_LON")
        or os.getenv("LON")
        or os.getenv("TAKPI_LONGITUDE")
    )
    # Also support LOCATION_LAT etc?
    lat_str = lat_str or os.getenv("LOCATION_LATITUDE")
    lon_str = lon_str or os.getenv("LOCATION_LONGITUDE")
    if lat_str and lon_str:
        try:
            lat = float(lat_str)
            lon = float(lon_str)
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                alt_str = os.getenv("TAKPI_ALTITUDE") or os.getenv("ALTITUDE")
                alt = float(alt_str) if alt_str else None
                return LocationConfig(latitude=lat, longitude=lon, altitude=alt)
            logger.warning("Env location out of range lat=%s lon=%s", lat_str, lon_str)
        except ValueError:
            logger.warning("Invalid env location lat=%r lon=%r", lat_str, lon_str)
    return None


def load_yaml_config(path: Path | None = None) -> TakpiConfig:
    """Load YAML config from ``path`` or default resolution. Returns empty config if missing/invalid."""
    if path is None:
        path = get_default_config_path()
    else:
        path = Path(path).expanduser()

    raw: dict[str, Any] = {}
    mtime: float | None = None
    if path.exists():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        try:
            import yaml  # type: ignore

            text = path.read_text(encoding="utf-8")
            if text.strip() == "":
                raw = {}
            else:
                loaded = yaml.safe_load(text)
                if loaded is None:
                    raw = {}
                elif isinstance(loaded, dict):
                    raw = loaded
                else:
                    logger.warning("Config %s is not a mapping, ignoring", path)
                    raw = {}
        except ImportError:
            logger.warning("PyYAML not installed – cannot parse %s", path)
            raw = {}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Failed to load config %s: %s", path, exc)
            raw = {}
    else:
        # No file – check env fallback for location still
        logger.debug("Config file %s not found, checking env fallback", path)

    # Extract location block
    loc_cfg = LocationConfig()
    loc_block = raw.get("location") if isinstance(raw.get("location"), dict) else None
    if loc_block:
        try:
            lat = loc_block.get("latitude")
            lon = loc_block.get("longitude")
            alt = loc_block.get("altitude")
            # Allow string numbers
            if lat is not None:
                lat = float(lat)  # type: ignore[arg-type]
            if lon is not None:
                lon = float(lon)  # type: ignore[arg-type]
            if alt is not None:
                alt = float(alt)  # type: ignore[arg-type]
            # Validate ranges
            if lat is not None and not (-90.0 <= lat <= 90.0):
                logger.warning("Config %s location.latitude %s out of range", path, lat)
                lat = None
            if lon is not None and not (-180.0 <= lon <= 180.0):
                logger.warning(
                    "Config %s location.longitude %s out of range", path, lon
                )
                lon = None
            if lat is not None and lon is not None:
                loc_cfg = LocationConfig(latitude=lat, longitude=lon, altitude=alt)
            elif lat is not None or lon is not None:
                logger.warning(
                    "Config %s location incomplete (need both lat/lon)", path
                )
        except (ValueError, TypeError) as exc:
            logger.warning("Invalid location in %s: %s", path, exc)

    # Env fallback if YAML has no valid location
    if not loc_cfg.is_valid():
        env_loc = _get_env_location()
        if env_loc and env_loc.is_valid():
            loc_cfg = env_loc
            logger.info("Using location from env fallback: %s", loc_cfg)

    return TakpiConfig(location=loc_cfg, raw=raw, path=path, mtime=mtime)


class ConfigManager:
    """Polling watcher for central YAML config.

    Example::

        mgr = ConfigManager()  # defaults to ~/takpi/config.yaml
        cfg = mgr.load()  # sync
        async with mgr:  # starts watcher
            while True:
                await asyncio.sleep(1)

    When used with :class:`takpi_common.location.LocationProvider`, the provider
    will poll :meth:`load` itself; this manager is optional helper that can
    publish ``ConfigChanged`` on an :class:`EventBus` if provided.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        poll_interval: float = 10.0,
        bus: Any | None = None,
    ) -> None:
        if path is None:
            self.path = get_default_config_path()
        else:
            self.path = Path(path).expanduser()
        self.poll_interval = poll_interval
        self.bus = bus
        self._config: TakpiConfig | None = None
        self._mtime: float | None = None
        self._task: Any | None = None
        self._running = False

    def load(self) -> TakpiConfig:
        """Load (or reload) config and update cached mtime."""
        cfg = load_yaml_config(self.path)
        self._config = cfg
        self._mtime = cfg.mtime
        return cfg

    @property
    def config(self) -> TakpiConfig | None:
        return self._config

    def get_location(self) -> tuple[float, float] | None:
        """Return current location tuple or None."""
        if self._config is None:
            self.load()
        assert self._config is not None
        return self._config.location.as_tuple()

    def has_file_changed(self) -> bool:
        """Check if file mtime differs from cached. Updates cache if changed (but does not reload)."""
        try:
            if not self.path.exists():
                # File deleted -> considered changed if we had mtime
                return self._mtime is not None
            cur = self.path.stat().st_mtime
            if self._mtime is None:
                return True
            return cur != self._mtime
        except OSError:
            return False

    def check_and_reload(self) -> TakpiConfig | None:
        """If file changed since last load, reload and return new config; else None."""
        if self.has_file_changed():
            new_cfg = load_yaml_config(self.path)
            # Only return if meaningful change (location or mtime)
            old_loc = self._config.location if self._config else None
            if new_cfg.mtime != self._mtime or new_cfg.location != old_loc:
                self._config = new_cfg
                self._mtime = new_cfg.mtime
                logger.info(
                    "Config reloaded from %s (mtime %s)", self.path, self._mtime
                )
                return new_cfg
            # Update mtime even if location same
            self._mtime = new_cfg.mtime
            self._config = new_cfg
        return None

    async def start(self) -> None:
        """Start polling watcher. Loads initial config."""
        if self._running:
            return
        self._running = True
        self.load()
        import asyncio

        self._task = asyncio.create_task(self._watch_loop(), name="config-watcher")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                import asyncio

                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def __aenter__(self) -> ConfigManager:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stop()

    async def _watch_loop(self) -> None:
        import asyncio

        while self._running:
            try:
                await asyncio.sleep(self.poll_interval)
                new_cfg = self.check_and_reload()
                if new_cfg and self.bus is not None:
                    # Optionally publish raw config change; define event locally to avoid circular
                    try:
                        from dataclasses import dataclass as _dc

                        @_dc(frozen=True)
                        class ConfigChanged:
                            config: TakpiConfig
                            path: Path

                        await self.bus.publish(
                            ConfigChanged(config=new_cfg, path=self.path)
                        )
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.exception("Config watch error: %s", exc)
