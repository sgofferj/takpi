"""takpi_common – shared framework for takpi hardware bridges."""

from takpi_common.bus import EventBus
from takpi_common.config_manager import (
    ConfigManager,
    TakpiConfig,
    get_default_config_path,
    load_yaml_config,
)
from takpi_common.health import report_health
from takpi_common.location import (
    LocationProvider,
    LocationUpdate,
    get_timezone_name,
    haversine_m,
    interval_for_speed,
    parse_gps_time,
    utc_to_local,
)
from takpi_common.location_cot import LocationCotBridge
from takpi_common.mcp23017.driver import MCP23017
from takpi_common.mcp23017.manager import HardwareManager
from takpi_common.weather import WeatherProvider, WeatherUpdate, is_in_finland

__all__ = [
    "EventBus",
    "MCP23017",
    "HardwareManager",
    "report_health",
    "ConfigManager",
    "TakpiConfig",
    "get_default_config_path",
    "load_yaml_config",
    "LocationProvider",
    "LocationUpdate",
    "LocationCotBridge",
    "WeatherProvider",
    "WeatherUpdate",
    "is_in_finland",
    "haversine_m",
    "interval_for_speed",
    "parse_gps_time",
    "utc_to_local",
    "get_timezone_name",
]
__version__ = "0.1.0"
