"""
Button / Encoder / LED abstractions on top of MCP23017 pins.

All hardware is modelled as data + event dataclasses. The ``HardwareManager``
owns polling, debouncing and quadrature decoding; this module only defines
types, configs and small helpers. No I2C access here – keep it pure for tests.

Wiring assumptions (per takpi docs):
  - Buttons: active-low with internal 100k pull-up (MCP23017 GPPU), to GND.
  - Encoders: quadrature A/B on two pins (also pull-up), optional push-button on third pin.
  - LEDs: active-high (MCP pin → resistor → LED → GND), output mode.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Events – published on EventBus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ButtonEvent:
    """Button state change. ``pressed=True`` means active (pulled to GND)."""

    id: str
    device_addr: int
    pin: int
    pressed: bool  # True = pressed (active), False = released
    timestamp: float = field(default_factory=time.monotonic)

    @property
    def released(self) -> bool:
        return not self.pressed


@dataclass(frozen=True)
class EncoderEvent:
    """Rotary encoder movement. ``delta`` is +1 (CW) or -1 (CCW), ``position`` is cumulative."""

    id: str
    device_addr: int
    delta: int  # +1 or -1 per detent
    position: int
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class EncoderButtonEvent:
    """Encoder push-button (same as ButtonEvent but with encoder id)."""

    id: str
    device_addr: int
    pin: int
    pressed: bool
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class LedCommand:
    """LED control command published on bus (main → hardware)."""

    id: str
    device_addr: int
    pin: int
    state: bool  # True=on, False=off
    blink_ms: int | None = None  # if set, hardware blinks with this period (0=steady)


@dataclass(frozen=True)
class LedState:
    """LED actual state after command (hardware → bus ack)."""

    id: str
    device_addr: int
    pin: int
    state: bool
    timestamp: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Configs – used by HardwareManager to wire pins
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ButtonConfig:
    id: str
    device_addr: int  # MCP23017 address 0x20-0x27
    pin: int  # 0-15 (0-7=GPA, 8-15=GPB)
    pullup: bool = True
    invert: bool = False  # if True, active-high
    debounce_ms: int = 50


@dataclass(frozen=True)
class EncoderConfig:
    id: str
    device_addr: int
    pin_a: int
    pin_b: int
    pin_button: int | None = None  # optional push
    pullup: bool = True
    # Position tracking
    initial_position: int = 0


@dataclass(frozen=True)
class LedConfig:
    id: str
    device_addr: int
    pin: int
    initial: bool = False
    invert: bool = False  # if LED is active-low (to VCC)


# ---------------------------------------------------------------------------
# Runtime state helpers – used internally by manager
# ---------------------------------------------------------------------------


class ButtonState:
    """Debounced button tracking (owned by manager, not public API)."""

    def __init__(self, cfg: ButtonConfig) -> None:
        self.cfg = cfg
        self.last_raw: bool | None = None
        self.last_stable: bool | None = None
        self.last_change_ts: float = 0.0
        self.pressed: bool = False

    def update(self, raw_high: bool, now: float) -> ButtonEvent | None:
        """
        Feed raw pin level (True=high, False=low) and return ButtonEvent on stable edge.
        Active = (raw == low) when pullup+active-low (default).
        """
        # Determine logical active: low = pressed when pullup and not invert
        if self.cfg.invert:
            active = raw_high
        else:
            # Active-low
            active = not raw_high

        if self.last_raw is None or active != self.last_raw:
            self.last_raw = active
            self.last_change_ts = now
            return None

        # Stable for debounce_ms?
        if now - self.last_change_ts < (self.cfg.debounce_ms / 1000.0):
            return None

        if self.last_stable is None:
            # First stable reading – emit if pressed, but don't duplicate
            self.last_stable = active
            self.pressed = active
            # Only emit press, not initial release, to avoid spam on boot
            if active:
                return ButtonEvent(
                    id=self.cfg.id,
                    device_addr=self.cfg.device_addr,
                    pin=self.cfg.pin,
                    pressed=True,
                    timestamp=now,
                )
            return None

        if active != self.last_stable:
            self.last_stable = active
            self.pressed = active
            return ButtonEvent(
                id=self.cfg.id,
                device_addr=self.cfg.device_addr,
                pin=self.cfg.pin,
                pressed=active,
                timestamp=now,
            )
        return None


# Quadrature table: index = prev<<2 | curr, value = delta (-1,0,+1)
# States encoded as (A,B) → 0..3: 00=0,01=1,11=3,10=2 (Gray)
_ENCODER_TABLE: dict[int, int] = {
    0b0000: 0,
    0b0001: 1,
    0b0010: -1,
    0b0011: 0,
    0b0100: -1,
    0b0101: 0,
    0b0110: 0,
    0b0111: 1,
    0b1000: 1,
    0b1001: 0,
    0b1010: 0,
    0b1011: -1,
    0b1100: 0,
    0b1101: -1,
    0b1110: 1,
    0b1111: 0,
}


class EncoderState:
    """Quadrature decoder for one encoder (polling)."""

    def __init__(self, cfg: EncoderConfig) -> None:
        self.cfg = cfg
        self.position: int = cfg.initial_position
        self._prev_state: int | None = None
        self._accum: int = (
            0  # accumulate 4 steps = 1 detent? For many encoders, 4 transitions = 1 click
        )

    def _read_state(self, a_high: bool, b_high: bool) -> int:
        # Pullup active-low: high=1, low=0, but encoder outputs are also pullup
        # So we keep as is: high=1, low=0
        a = 1 if a_high else 0
        b = 1 if b_high else 0
        return (a << 1) | b  # A as high bit to match typical

    def update(self, a_high: bool, b_high: bool, now: float) -> EncoderEvent | None:
        curr = self._read_state(a_high, b_high)
        if self._prev_state is None:
            self._prev_state = curr
            return None
        prev = self._prev_state
        key = (prev << 2) | curr
        delta = _ENCODER_TABLE.get(key, 0)
        self._prev_state = curr
        if delta == 0:
            return None
        # Many encoders generate 4 edges per detent; we emit per edge for low latency
        # If you want per-detent, accumulate 4: self._accum += delta; if abs>=4: emit
        self.position += delta
        return EncoderEvent(
            id=self.cfg.id,
            device_addr=self.cfg.device_addr,
            delta=delta,
            position=self.position,
            timestamp=now,
        )


# Legacy simple helper for LED state mapping
@dataclass
class LedRuntime:
    cfg: LedConfig
    state: bool = False

    def apply(self, cmd: LedCommand) -> bool:
        """Update state from command, return True if changed."""
        eff = cmd.state ^ self.cfg.invert
        if eff != self.state:
            self.state = eff
            return True
        return False
