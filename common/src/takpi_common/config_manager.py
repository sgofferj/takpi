"""Central YAML config manager for takpi.

Resolves ``~/takpi/config.yaml`` (default) or ``TAKPI_CONFIG`` override,
loads ``location`` and ``hardware`` (gpio header + mcp23017) and watches for changes.

Hardware: header pin numbers (1..40, physical) are used in YAML – never BCM.
The manager translates header → BCM via :data:`HEADER_TO_BCM` for internal use.

Example ``config.yaml``:

.. code-block:: yaml

    location:
      latitude: 60.1699
      longitude: 24.9384

    hardware:
      gpio:
        8: gauge_tx
        10: gauge_rx
        13: escpos_tx
        36: gps_ind
      mcp23017:
        0x20:
          GPA0: btn_wipe
          GPA1: gps_ind
          GPB0: enc_main_a
          GPB1: enc_main_b

Modules register keywords via :func:`register_hardware_keyword`::

    from takpi_common.config_manager import register_hardware_keyword, HardwareSpec

    register_hardware_keyword("gps_ind", HardwareSpec(buses=["gpio","mcp"], type="led", module="location"))
    register_hardware_keyword("btn_*", HardwareSpec(buses=["mcp","gpio"], type="button", module="app"))

Config manager validates each entry against the registry, logs warning and
skips only the offending entry – transparent for user (per spec). Missing
``hardware`` section means disabled, not error.

File is polled for ``mtime`` changes by :class:`ConfigManager` or by callers
like :class:`takpi_common.location.LocationProvider` when gpsd is absent.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / "takpi" / "config.yaml"
FALLBACK_CONFIG_PATH = Path("/etc/takpi/config.yaml")
ENV_CONFIG_VAR = "TAKPI_CONFIG"

# ---------------------------------------------------------------------------
# Header (physical 1..40) → BCM mapping – None for power/GND
# ---------------------------------------------------------------------------
HEADER_TO_BCM: dict[int, int | None] = {
    1: None,  # 3.3V
    2: None,  # 5V
    3: 2,  # GPIO2 SDA1
    4: None,  # 5V
    5: 3,  # GPIO3 SCL1
    6: None,  # GND
    7: 4,  # GPIO4
    8: 14,  # GPIO14 TXD0
    9: None,  # GND
    10: 15,  # GPIO15 RXD0
    11: 17,  # GPIO17
    12: 18,  # GPIO18
    13: 27,  # GPIO27
    14: None,  # GND
    15: 22,  # GPIO22
    16: 23,  # GPIO23
    17: None,  # 3.3V
    18: 24,  # GPIO24
    19: 10,  # GPIO10 MOSI
    20: None,  # GND
    21: 9,  # GPIO9 MISO
    22: 25,  # GPIO25
    23: 11,  # GPIO11 SCLK
    24: 8,  # GPIO8 CE0
    25: None,  # GND
    26: 7,  # GPIO7 CE1
    27: 0,  # GPIO0 ID_SDA
    28: 1,  # GPIO1 ID_SCL
    29: 5,  # GPIO5
    30: None,  # GND
    31: 6,  # GPIO6
    32: 12,  # GPIO12
    33: 13,  # GPIO13
    34: None,  # GND
    35: 19,  # GPIO19
    36: 16,  # GPIO16
    37: 26,  # GPIO26
    38: 20,  # GPIO20
    39: None,  # GND
    40: 21,  # GPIO21
}

BCM_TO_HEADER: dict[int, int] = {
    b: h for h, b in HEADER_TO_BCM.items() if b is not None
}


def header_to_bcm(header_pin: int) -> int | None:
    """Translate physical header pin (1..40) → BCM, or None for power/GND/invalid."""
    return HEADER_TO_BCM.get(header_pin)


def bcm_to_header(bcm: int) -> int | None:
    """Translate BCM → header pin, or None."""
    return BCM_TO_HEADER.get(bcm)


def is_valid_header_pin(pin: int) -> bool:
    """True if pin is a valid GPIO header pin (has BCM mapping)."""
    return HEADER_TO_BCM.get(pin) is not None


# ---------------------------------------------------------------------------
# Hardware registry – modules register keywords they understand
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HardwareSpec:
    """Specification for a hardware keyword.

    Attributes:
        buses: allowed buses, subset of {"gpio","mcp"}
        type: logical type, e.g. "button","led","encoder_a","uart_tx"
        module: owning module name for diagnostics
        description: human readable
    """

    buses: tuple[str, ...] = ("gpio", "mcp")
    type: str = "generic"
    module: str = "unknown"
    description: str = ""


# Global registry: pattern (exact or fnmatch with *) → spec
_HARDWARE_REGISTRY: dict[str, HardwareSpec] = {}


def register_hardware_keyword(keyword: str, spec: HardwareSpec) -> None:
    """Register a keyword (exact or wildcard ``*``) that a module understands.

    Example::

        register_hardware_keyword("gps_ind", HardwareSpec(buses=("gpio","mcp"), type="led", module="location"))
        register_hardware_keyword("btn_*", HardwareSpec(buses=("mcp","gpio"), type="button", module="app"))

    If keyword already registered, it is overwritten (last wins, warns).
    """
    if keyword in _HARDWARE_REGISTRY:
        logger.debug("Overwriting hardware keyword %r", keyword)
    _HARDWARE_REGISTRY[keyword] = spec
    logger.debug("Registered hardware keyword %r → %s", keyword, spec)


def _find_spec(keyword: str) -> HardwareSpec | None:
    """Find spec for keyword, supporting wildcard patterns (`*`). Exact match preferred."""
    # Exact first
    if keyword in _HARDWARE_REGISTRY:
        return _HARDWARE_REGISTRY[keyword]
    # Wildcard
    for pat, spec in _HARDWARE_REGISTRY.items():
        if "*" in pat and fnmatch.fnmatch(keyword, pat):
            return spec
    return None


def is_keyword_registered(keyword: str) -> bool:
    """Return True if keyword is registered (exact or wildcard)."""
    return _find_spec(keyword) is not None


def get_registered_keywords() -> dict[str, HardwareSpec]:
    """Return copy of hardware registry."""
    return dict(_HARDWARE_REGISTRY)


# Pre-register common generic patterns so arbitrary btn_/led_/enc_ work out of the box
# These are low-priority fallbacks; specific modules can override with exact keywords.
register_hardware_keyword(
    "btn_*",
    HardwareSpec(
        buses=("mcp", "gpio"),
        type="button",
        module="common",
        description="generic button",
    ),
)
register_hardware_keyword(
    "led_*",
    HardwareSpec(
        buses=("mcp", "gpio"), type="led", module="common", description="generic LED"
    ),
)
register_hardware_keyword(
    "*_ind",
    HardwareSpec(
        buses=("mcp", "gpio"), type="led", module="common", description="indicator LED"
    ),
)
register_hardware_keyword(
    "enc_*_a",
    HardwareSpec(
        buses=("mcp",), type="encoder_a", module="common", description="encoder A"
    ),
)
register_hardware_keyword(
    "enc_*_b",
    HardwareSpec(
        buses=("mcp",), type="encoder_b", module="common", description="encoder B"
    ),
)
register_hardware_keyword(
    "enc_*_btn",
    HardwareSpec(
        buses=("mcp",),
        type="encoder_btn",
        module="common",
        description="encoder button",
    ),
)
register_hardware_keyword(
    "enc_*",
    HardwareSpec(
        buses=("mcp",), type="encoder", module="common", description="encoder"
    ),
)

# Specific module keywords (header pins, per spec)
register_hardware_keyword(
    "gauge_tx",
    HardwareSpec(
        buses=("gpio",),
        type="uart_tx",
        module="tak-bridge-chronometer",
        description="GSA-072 TX (header 8 → BCM14)",
    ),
)
register_hardware_keyword(
    "gauge_rx",
    HardwareSpec(
        buses=("gpio",),
        type="uart_rx",
        module="tak-bridge-chronometer",
        description="GSA-072 RX (header 10 → BCM15)",
    ),
)
register_hardware_keyword(
    "escpos_tx",
    HardwareSpec(
        buses=("gpio",),
        type="uart_tx",
        module="tak-bridge-escpos",
        description="ESC/POS TX softserial",
    ),
)
register_hardware_keyword(
    "escpos_rx",
    HardwareSpec(
        buses=("gpio",),
        type="uart_rx",
        module="tak-bridge-escpos",
        description="ESC/POS RX softserial",
    ),
)
register_hardware_keyword(
    "wipe",
    HardwareSpec(
        buses=("gpio", "mcp"),
        type="button",
        module="common",
        description="wipe button – recommended on GPIO header (e.g. 18) for reliability, MCP fallback",
    ),
)
register_hardware_keyword(
    "gps_ind",
    HardwareSpec(
        buses=("gpio", "mcp"),
        type="led",
        module="location",
        description="GPS fix indicator",
    ),
)
register_hardware_keyword(
    "gps_fix",
    HardwareSpec(
        buses=("gpio", "mcp"),
        type="led",
        module="location",
        description="GPS fix indicator",
    ),
)
register_hardware_keyword(
    "led_wipe_avail",
    HardwareSpec(
        buses=("gpio", "mcp"),
        type="led",
        module="wipe",
        description="Wipe available indicator (on when wipe ready)",
    ),
)
register_hardware_keyword(
    "led_wipe_trig",
    HardwareSpec(
        buses=("gpio", "mcp"),
        type="led",
        module="wipe",
        description="Wipe triggered indicator (on during wipe, off on poweroff)",
    ),
)
register_hardware_keyword(
    "LED_WIPE_AVAIL",
    HardwareSpec(
        buses=("gpio", "mcp"),
        type="led",
        module="wipe",
        description="Wipe available (alias)",
    ),
)
register_hardware_keyword(
    "LED_WIPE_TRIG",
    HardwareSpec(
        buses=("gpio", "mcp"),
        type="led",
        module="wipe",
        description="Wipe triggered (alias)",
    ),
)


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Hardware assignments (normalized)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GpioAssignment:
    header_pin: int
    bcm: int
    keyword: str
    spec: HardwareSpec | None = None


@dataclass(frozen=True)
class McpAssignment:
    address: int  # 0x20..0x27
    pin_name: str  # GPA0..GPB7
    pin_index: int  # 0..15
    keyword: str
    spec: HardwareSpec | None = None


@dataclass
class HardwareConfigNormalized:
    """Normalized hardware assignments after validation (only valid entries kept)."""

    gpio: list[GpioAssignment] = field(default_factory=list)
    mcp: list[McpAssignment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_MCP_PIN_RE = re.compile(r"^GP([AB])([0-7])$", re.IGNORECASE)


def _parse_mcp_pin(pin_name: str) -> int | None:
    """GPA0..GPB7 → 0..15, or None if invalid."""
    m = _MCP_PIN_RE.match(pin_name.strip())
    if not m:
        return None
    port = m.group(1).upper()
    bit = int(m.group(2))
    return bit if port == "A" else 8 + bit


def _parse_gpio_section(
    gpio_raw: Any,
    path: Path,
    result: HardwareConfigNormalized,
    used_gpio: dict[int, str],
) -> None:
    """Parse hardware.gpio section."""
    if gpio_raw is None:
        return
    if not isinstance(gpio_raw, dict):
        msg = f"Config {path} hardware.gpio is not a mapping, ignoring"
        logger.warning(msg)
        result.warnings.append(msg)
        return
    for k, v in gpio_raw.items():
        try:
            header_pin = int(str(k).strip())
        except ValueError:
            msg = f"Config {path} hardware.gpio key {k!r} is not an integer header pin, skipping"
            logger.warning(msg)
            result.warnings.append(msg)
            continue
        if not 1 <= header_pin <= 40:
            msg = f"Config {path} hardware.gpio pin {header_pin} out of range 1..40, skipping"
            logger.warning(msg)
            result.warnings.append(msg)
            continue
        bcm = header_to_bcm(header_pin)
        if bcm is None:
            msg = f"Config {path} hardware.gpio pin {header_pin} is power/GND, not GPIO, skipping"
            logger.warning(msg)
            result.warnings.append(msg)
            continue
        if not isinstance(v, str) or not v.strip():
            msg = f"Config {path} hardware.gpio pin {header_pin} keyword {v!r} invalid, skipping"
            logger.warning(msg)
            result.warnings.append(msg)
            continue
        keyword = v.strip()
        if header_pin in used_gpio:
            msg = f"Config {path} hardware.gpio pin {header_pin} duplicate keyword {keyword!r} (already {used_gpio[header_pin]!r}), skipping"
            logger.warning(msg)
            result.warnings.append(msg)
            continue
        spec = _find_spec(keyword)
        if spec is None:
            msg = f"Config {path} hardware.gpio pin {header_pin} keyword {keyword!r} not registered by any module, skipping (unknown)"
            logger.warning(msg)
            result.warnings.append(msg)
            continue
        if "gpio" not in spec.buses:
            msg = f"Config {path} hardware.gpio pin {header_pin} keyword {keyword!r} not allowed on gpio (spec buses {spec.buses}), skipping"
            logger.warning(msg)
            result.warnings.append(msg)
            continue
        for existing in result.gpio:
            if existing.keyword == keyword:
                logger.warning(
                    "Config %s hardware.gpio keyword %r used on multiple pins (%d and %d)",
                    path,
                    keyword,
                    existing.header_pin,
                    header_pin,
                )
                break
        used_gpio[header_pin] = keyword
        result.gpio.append(
            GpioAssignment(header_pin=header_pin, bcm=bcm, keyword=keyword, spec=spec)
        )


def _parse_mcp_section(
    mcp_raw: Any,
    path: Path,
    result: HardwareConfigNormalized,
    used_mcp: dict[tuple[int, str], str],
) -> None:
    """Parse hardware.mcp23017 section."""
    if mcp_raw is None:
        return
    if not isinstance(mcp_raw, dict):
        msg = f"Config {path} hardware.mcp23017 is not a mapping, ignoring"
        logger.warning(msg)
        result.warnings.append(msg)
        return
    for addr_k, pins_v in mcp_raw.items():
        try:
            addr_str = str(addr_k).strip()
            address = int(addr_str, 0)  # auto base
        except ValueError:
            msg = f"Config {path} hardware.mcp23017 key {addr_k!r} not a valid address, skipping"
            logger.warning(msg)
            result.warnings.append(msg)
            continue
        if not 0x20 <= address <= 0x27:
            msg = f"Config {path} hardware.mcp23017 address {addr_k!r} ({hex(address)}) out of range 0x20..0x27, skipping"
            logger.warning(msg)
            result.warnings.append(msg)
            continue
        if not isinstance(pins_v, dict):
            msg = f"Config {path} hardware.mcp23017 {hex(address)} value is not a mapping, skipping"
            logger.warning(msg)
            result.warnings.append(msg)
            continue
        for pin_name_raw, keyword_raw in pins_v.items():
            pin_name = str(pin_name_raw).strip()
            pin_idx = _parse_mcp_pin(pin_name)
            if pin_idx is None:
                msg = f"Config {path} hardware.mcp23017 {hex(address)} pin {pin_name!r} invalid (expect GPA0..GPB7), skipping"
                logger.warning(msg)
                result.warnings.append(msg)
                continue
            pin_name = pin_name.upper()
            if pin_name not in (
                "GPA0",
                "GPA1",
                "GPA2",
                "GPA3",
                "GPA4",
                "GPA5",
                "GPA6",
                "GPA7",
                "GPB0",
                "GPB1",
                "GPB2",
                "GPB3",
                "GPB4",
                "GPB5",
                "GPB6",
                "GPB7",
            ):
                pass
            if not isinstance(keyword_raw, str) or not keyword_raw.strip():
                msg = f"Config {path} hardware.mcp23017 {hex(address)} {pin_name} keyword {keyword_raw!r} invalid, skipping"
                logger.warning(msg)
                result.warnings.append(msg)
                continue
            keyword = keyword_raw.strip()
            key = (address, pin_name)
            if key in used_mcp:
                msg = f"Config {path} hardware.mcp23017 {hex(address)} {pin_name} duplicate, skipping"
                logger.warning(msg)
                result.warnings.append(msg)
                continue
            spec = _find_spec(keyword)
            if spec is None:
                msg = f"Config {path} hardware.mcp23017 {hex(address)} {pin_name} keyword {keyword!r} not registered, skipping"
                logger.warning(msg)
                result.warnings.append(msg)
                continue
            if "mcp" not in spec.buses:
                msg = f"Config {path} hardware.mcp23017 {hex(address)} {pin_name} keyword {keyword!r} not allowed on mcp (buses {spec.buses}), skipping"
                logger.warning(msg)
                result.warnings.append(msg)
                continue
            used_mcp[key] = keyword
            result.mcp.append(
                McpAssignment(
                    address=address,
                    pin_name=pin_name,
                    pin_index=pin_idx,
                    keyword=keyword,
                    spec=spec,
                )
            )


def _parse_hardware_section(raw: Any, path: Path) -> HardwareConfigNormalized:
    """Parse raw ``hardware`` dict from YAML, validate against registry, collect warnings."""
    result = HardwareConfigNormalized()
    if raw is None:
        return result
    if not isinstance(raw, dict):
        msg = f"Config {path} hardware is not a mapping, ignoring"
        logger.warning(msg)
        result.warnings.append(msg)
        return result

    used_gpio: dict[int, str] = {}
    used_mcp: dict[tuple[int, str], str] = {}
    _parse_gpio_section(raw.get("gpio"), path, result, used_gpio)
    _parse_mcp_section(raw.get("mcp23017") or raw.get("mcp"), path, result, used_mcp)
    return result


@dataclass
class TakpiConfig:
    location: LocationConfig = field(default_factory=LocationConfig)
    hardware: HardwareConfigNormalized = field(default_factory=HardwareConfigNormalized)
    production: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None
    mtime: float | None = None

    def has_location(self) -> bool:
        return self.location.is_valid()

    def is_production(self) -> bool:
        """Return True if production mode (logging disabled)."""
        return self.production or is_production_env()

    def get_gpio_pin(self, keyword: str) -> GpioAssignment | None:
        for a in self.hardware.gpio:
            if a.keyword == keyword:
                return a
        return None

    def get_mcp_pin(self, keyword: str) -> McpAssignment | None:
        for a in self.hardware.mcp:
            if a.keyword == keyword:
                return a
        return None

    def get_all_keywords(self) -> list[str]:
        return [a.keyword for a in self.hardware.gpio] + [
            a.keyword for a in self.hardware.mcp
        ]


def is_production_env() -> bool:
    """Check env vars for production mode (TAKPI_PRODUCTION, PRODUCTION)."""
    for var in ("TAKPI_PRODUCTION", "PRODUCTION", "TAKPI_ENV"):
        val = os.getenv(var, "").strip().lower()
        if val in ("1", "true", "yes", "on", "prod", "production"):
            return True
    return False


def apply_production_logging(disable: bool = True) -> None:
    """Disable all logging if production mode (per spec)."""
    if disable:
        logging.disable(logging.CRITICAL)
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
    else:
        logging.disable(logging.NOTSET)


def get_default_config_path() -> Path:
    """Resolve central config path: ``TAKPI_CONFIG`` > ``~/takpi/config.yaml`` > ``/etc/takpi/config.yaml``."""
    env_path = os.getenv(ENV_CONFIG_VAR)
    if env_path:
        p = Path(env_path).expanduser()
        return p
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    if FALLBACK_CONFIG_PATH.exists():
        return FALLBACK_CONFIG_PATH
    return DEFAULT_CONFIG_PATH


def _get_env_location() -> LocationConfig | None:
    """Legacy env fallback: TAK_LAT/TAK_LON or LAT/LON or TAKPI_LAT/LON."""
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
        logger.debug("Config file %s not found, checking env fallback", path)

    # Extract location block
    loc_cfg = LocationConfig()
    loc_block = raw.get("location") if isinstance(raw.get("location"), dict) else None
    if loc_block:
        try:
            lat = loc_block.get("latitude")
            lon = loc_block.get("longitude")
            alt = loc_block.get("altitude")
            if lat is not None:
                lat = float(lat)  # type: ignore[arg-type]
            if lon is not None:
                lon = float(lon)  # type: ignore[arg-type]
            if alt is not None:
                alt = float(alt)  # type: ignore[arg-type]
            if lat is not None and (-90.0 > lat or lat > 90.0):
                logger.warning("Config %s location.latitude %s out of range", path, lat)
                lat = None
            if lon is not None and (-180.0 > lon or lon > 180.0):
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

    if not loc_cfg.is_valid():
        env_loc = _get_env_location()
        if env_loc and env_loc.is_valid():
            loc_cfg = env_loc
            logger.info("Using location from env fallback: %s", loc_cfg)

    # Parse production flag (disables logging completely)
    production = False
    prod_raw = raw.get("production")
    if isinstance(prod_raw, bool):
        production = prod_raw
    elif isinstance(prod_raw, str):
        production = prod_raw.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(prod_raw, int):
        production = bool(prod_raw)
    if is_production_env():
        production = True
    if production:
        apply_production_logging(True)

    # Parse hardware section
    hardware_cfg = _parse_hardware_section(raw.get("hardware"), path)

    return TakpiConfig(
        location=loc_cfg,
        hardware=hardware_cfg,
        production=production,
        raw=raw,
        path=path,
        mtime=mtime,
    )


class ConfigManager:
    """Polling watcher for central YAML config."""

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
        """Check if file mtime differs from cached."""
        try:
            if not self.path.exists():
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
            old_loc = self._config.location if self._config else None
            old_hw = self._config.hardware if self._config else None
            old_prod = self._config.production if self._config else None
            # Check if anything meaningful changed
            if (
                new_cfg.mtime != self._mtime
                or new_cfg.location != old_loc
                or new_cfg.hardware != old_hw
                or new_cfg.production != old_prod
            ):
                self._config = new_cfg
                self._mtime = new_cfg.mtime
                logger.info(
                    "Config reloaded from %s (mtime %s)", self.path, self._mtime
                )
                return new_cfg
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
        import asyncio  # pylint: disable=import-outside-toplevel

        while self._running:
            try:
                await asyncio.sleep(self.poll_interval)
                new_cfg = self.check_and_reload()
                if new_cfg and self.bus is not None:
                    try:

                        @dataclass(
                            frozen=True
                        )  # pylint: disable=too-few-public-methods
                        class ConfigChanged:
                            """Internal event for config reload."""

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
