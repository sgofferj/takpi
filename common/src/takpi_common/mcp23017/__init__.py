"""MCP23017 daisy-chain framework: driver + button/encoder/LED abstractions + manager."""

from takpi_common.mcp23017.driver import MCP23017, FakeSMBus
from takpi_common.mcp23017.io import (
    ButtonConfig,
    ButtonEvent,
    ButtonState,
    EncoderButtonEvent,
    EncoderConfig,
    EncoderEvent,
    EncoderState,
    LedCommand,
    LedConfig,
    LedRuntime,
    LedState,
)
from takpi_common.mcp23017.manager import HardwareManager, HardwareConfig

__all__ = [
    "MCP23017",
    "FakeSMBus",
    "ButtonConfig",
    "ButtonEvent",
    "ButtonState",
    "EncoderButtonEvent",
    "EncoderConfig",
    "EncoderEvent",
    "EncoderState",
    "LedCommand",
    "LedConfig",
    "LedRuntime",
    "LedState",
    "HardwareManager",
    "HardwareConfig",
]
