"""
HardwareManager – orchestrates daisy-chained MCP23017s and bridges to EventBus.

Polls GPIO inputs (buttons/encoders) at high frequency and publishes typed
events on the bus. Subscribes to ``LedCommand`` from the bus and drives
MCP23017 outputs.

Bidirectional flow (per requirement):
  hardware (button/encoder) ──publish──► EventBus ──► main process (CoT, etc.)
  main process ──publish LedCommand──► EventBus ──► manager ──► MCP23017 LED

Supports:
  - Multiple MCP23017 at 0x20-0x27 on one I2C bus (daisy chain, common SCL/SDA)
  - Polling (default 5 ms) and optional GPIO interrupt (INTA/INTB → Pi BCM pin)
  - Debounce, pull-ups, active-low, quadrature decoding
  - LED steady + optional blink via asyncio task
  - FakeSMBus for CI without hardware
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from typing import Any

from takpi_common.bus import EventBus
from takpi_common.mcp23017.driver import FakeSMBus, MCP23017
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

logger = logging.getLogger(__name__)


def _encoder_base(keyword: str) -> str:
    """Strip _a/_b/_btn suffix for encoder grouping, e.g. enc_main_a → enc_main"""
    for suf in ("_a", "_b", "_btn"):
        if keyword.endswith(suf):
            return keyword[: -len(suf)]
    # also handle enc_ prefix without suffix? fallback whole
    return keyword


@dataclass(frozen=True)
class HardwareConfig:
    """Declarative wiring for a daisy-chained MCP23017 chain."""

    i2c_bus: int = 1
    devices: list[int] = field(default_factory=lambda: [0x20])  # addresses
    buttons: list[ButtonConfig] = field(default_factory=list)
    encoders: list[EncoderConfig] = field(default_factory=list)
    leds: list[LedConfig] = field(default_factory=list)
    poll_interval_ms: int = 5  # 5 ms → 200 Hz, enough for encoder detents
    debounce_ms: int = 50  # default, overridden per-button
    interrupt_gpio: int | None = None  # BCM pin wired to MCP INT (future)
    # Optional: bus instance injection for tests
    smbus: object | None = None  # FakeSMBus or smbus2.SMBus

    @classmethod
    def from_takpi_config(
        cls,
        takpi_cfg: Any,
        *,
        i2c_bus: int = 1,
        poll_interval_ms: int = 5,
        smbus: Any | None = None,
    ) -> HardwareConfig:
        """Build HardwareConfig from central ``TakpiConfig`` hardware section.

        Only valid entries (already validated by ConfigManager) are used.
        Missing hardware section → empty config (disabled, per spec).
        Encoder grouping: keywords ``enc_foo_a``/``enc_foo_b``/``enc_foo_btn`` on same
        address are grouped into one ``EncoderConfig`` (id=enc_foo).
        """
        # Avoid circular import at runtime
        hw = getattr(takpi_cfg, "hardware", None)
        if hw is None:
            return cls(
                i2c_bus=i2c_bus,
                devices=[],
                buttons=[],
                encoders=[],
                leds=[],
                poll_interval_ms=poll_interval_ms,
                smbus=smbus,
            )
        mcp_list = getattr(hw, "mcp", []) or []
        if not mcp_list:
            return cls(
                i2c_bus=i2c_bus,
                devices=[],
                buttons=[],
                encoders=[],
                leds=[],
                poll_interval_ms=poll_interval_ms,
                smbus=smbus,
            )

        from collections import defaultdict

        # Collect devices
        devices_set = {a.address for a in mcp_list}
        devices = sorted(devices_set)

        buttons: list[ButtonConfig] = []
        leds: list[LedConfig] = []
        # Encoder grouping: (address, base) -> dict
        enc_groups: dict[tuple[int, str], dict[str, Any]] = defaultdict(dict)

        for a in mcp_list:
            spec = getattr(a, "spec", None)
            spec_type = getattr(spec, "type", "generic") if spec else "generic"
            keyword = a.keyword
            # Normalize type inference if generic
            # Use spec_type if not generic, else infer from keyword
            inferred = spec_type
            if inferred == "generic":
                lk = keyword.lower()
                if (
                    lk.startswith("btn_")
                    or lk == "wipe"
                    or lk.endswith("_btn")
                    and "enc_" not in lk
                ):
                    inferred = "button"
                elif lk.startswith("led_") or lk.endswith("_ind"):
                    inferred = "led"
                elif lk.startswith("enc_"):
                    # will be handled as encoder grouping
                    inferred = "encoder"
                else:
                    # Fallback: treat as led for *_ind, else button? Safer as button for inputs
                    inferred = "button"

            if inferred == "button":
                buttons.append(
                    ButtonConfig(id=keyword, device_addr=a.address, pin=a.pin_index)
                )
            elif inferred == "led":
                leds.append(
                    LedConfig(id=keyword, device_addr=a.address, pin=a.pin_index)
                )
            elif inferred in ("encoder_a", "encoder_b", "encoder_btn", "encoder"):
                # Determine base and subtype
                base = _encoder_base(keyword)
                grp_key = (a.address, base)
                grp = enc_groups[grp_key]
                # store address for later (should be same for group)
                grp["address"] = a.address
                grp["id"] = base
                if inferred == "encoder_a" or keyword.endswith("_a"):
                    grp["pin_a"] = a.pin_index
                elif inferred == "encoder_b" or keyword.endswith("_b"):
                    grp["pin_b"] = a.pin_index
                elif inferred == "encoder_btn" or keyword.endswith("_btn"):
                    grp["pin_button"] = a.pin_index
                else:
                    # Generic encoder without suffix – try to assign a/b in order
                    if "pin_a" not in grp:
                        grp["pin_a"] = a.pin_index
                    elif "pin_b" not in grp:
                        grp["pin_b"] = a.pin_index
            else:
                # Unknown type – treat as button if input-like, else led
                logger.warning(
                    "Unknown hardware spec type %r for keyword %r, treating as button",
                    inferred,
                    keyword,
                )
                buttons.append(
                    ButtonConfig(id=keyword, device_addr=a.address, pin=a.pin_index)
                )

        encoders: list[EncoderConfig] = []
        for (addr, base), grp in enc_groups.items():
            if "pin_a" in grp and "pin_b" in grp:
                encoders.append(
                    EncoderConfig(
                        id=base,
                        device_addr=addr,
                        pin_a=grp["pin_a"],
                        pin_b=grp["pin_b"],
                        pin_button=grp.get("pin_button"),
                    )
                )
            else:
                logger.warning(
                    "Encoder group %r on %s incomplete (need pin_a+pin_b), skipping",
                    base,
                    hex(addr),
                )

        return cls(
            i2c_bus=i2c_bus,
            devices=devices,
            buttons=buttons,
            encoders=encoders,
            leds=leds,
            poll_interval_ms=poll_interval_ms,
            smbus=smbus,
        )


class HardwareManager:
    """
    Async manager for MCP23017 chain.

    Example::

        bus = EventBus()
        cfg = HardwareConfig(
            devices=[0x20, 0x21],
            buttons=[ButtonConfig(id="btn1", device_addr=0x20, pin=0)],
            encoders=[EncoderConfig(id="enc1", device_addr=0x20, pin_a=1, pin_b=2)],
            leds=[LedConfig(id="alert_led", device_addr=0x21, pin=0)],
        )
        mgr = HardwareManager(cfg, bus)
        await mgr.start()

        # hardware → main
        bus.subscribe(ButtonEvent, handle_button)
        bus.subscribe(EncoderEvent, handle_encoder)

        # main → hardware
        await bus.publish(LedCommand(id="alert_led", device_addr=0x21, pin=0, state=True))
    """

    def __init__(self, config: HardwareConfig, bus: EventBus) -> None:
        """__init__."""
        self.cfg = config
        self.bus = bus
        self._devices: dict[int, MCP23017] = {}
        self._button_states: dict[str, ButtonState] = {}
        self._encoder_states: dict[str, EncoderState] = {}
        self._encoder_button_states: dict[str, ButtonState] = {}
        self._leds: dict[str, LedRuntime] = {}
        self._task: asyncio.Task[None] | None = None
        self._blink_tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False
        self._i2c_lock = asyncio.Lock()

        # Prepare bus instance (shared across devices)
        smbus_obj = config.smbus
        if smbus_obj is None and config.devices:
            # Create one bus for all devices; individual MCP23017 will share via injection
            # If smbus2 not installed and no injection, driver will raise on first use (lazy)
            smbus_obj = None  # lazy inside MCP23017._bus()

        for addr in config.devices:
            self._devices[addr] = MCP23017(
                address=addr, bus=config.i2c_bus, smbus=smbus_obj
            )

        for bc in config.buttons:
            self._button_states[bc.id] = ButtonState(bc)

        for ec in config.encoders:
            self._encoder_states[ec.id] = EncoderState(ec)
            if ec.pin_button is not None:
                btn_cfg = ButtonConfig(
                    id=f"{ec.id}_btn",
                    device_addr=ec.device_addr,
                    pin=ec.pin_button,
                    pullup=ec.pullup,
                    debounce_ms=config.debounce_ms,
                )
                self._encoder_button_states[ec.id] = ButtonState(btn_cfg)

        for lc in config.leds:
            self._leds[lc.id] = LedRuntime(cfg=lc, state=lc.initial)

        self._unsub_led: Any = None

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        """Configure MCP23017s and start polling + LED subscription."""
        if self._running:
            return
        self._running = True

        # Configure each device: directions + pullups + initial LED states
        for addr, dev in self._devices.items():
            await self._run_in_executor(dev.setup)
            # Buttons/encoders → inputs with pullup
            for bc in self.cfg.buttons:
                if bc.device_addr == addr:
                    await self._run_in_executor(dev.set_direction, bc.pin, True)
                    await self._run_in_executor(dev.set_pullup, bc.pin, bc.pullup)
            for ec in self.cfg.encoders:
                if ec.device_addr == addr:
                    await self._run_in_executor(dev.set_direction, ec.pin_a, True)
                    await self._run_in_executor(dev.set_direction, ec.pin_b, True)
                    await self._run_in_executor(dev.set_pullup, ec.pin_a, ec.pullup)
                    await self._run_in_executor(dev.set_pullup, ec.pin_b, ec.pullup)
                    if ec.pin_button is not None:
                        await self._run_in_executor(
                            dev.set_direction, ec.pin_button, True
                        )
                        await self._run_in_executor(dev.set_pullup, ec.pin_button, True)
            for lc in self.cfg.leds:
                if lc.device_addr == addr:
                    await self._run_in_executor(dev.set_direction, lc.pin, False)
                    # Set initial state
                    rt = self._leds[lc.id]
                    await self._run_in_executor(
                        dev.write_pin, lc.pin, rt.state ^ lc.invert
                    )

        # Subscribe to LED commands (main → hardware)
        self._unsub_led = self.bus.subscribe(LedCommand, self._handle_led_command)

        self._task = asyncio.create_task(self._poll_loop(), name="mcp23017-poll")
        logger.info(
            "HardwareManager started on I2C bus %d with %d devices, %d buttons, %d encoders, %d leds (poll %d ms)",
            self.cfg.i2c_bus,
            len(self._devices),
            len(self._button_states),
            len(self._encoder_states),
            len(self._leds),
            self.cfg.poll_interval_ms,
        )

    async def stop(self) -> None:
        """Stop polling and cleanup."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._unsub_led:
            self._unsub_led()
            self._unsub_led = None
        for t in list(self._blink_tasks.values()):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._blink_tasks.clear()
        for dev in self._devices.values():
            try:
                dev.close()
            except Exception:
                pass
        logger.info("HardwareManager stopped")

    async def __aenter__(self) -> HardwareManager:
        """__aenter__."""
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        """__aexit__."""
        await self.stop()

    # -- I2C helper --------------------------------------------------------------

    async def _run_in_executor(self, func: Any, *args: Any) -> Any:
        """_run_in_executor."""
        loop = asyncio.get_running_loop()
        async with self._i2c_lock:
            return await loop.run_in_executor(None, func, *args)

    # -- LED handling (main → hardware) ----------------------------------------

    async def _handle_led_command(self, cmd: LedCommand) -> None:
        """_handle_led_command."""
        rt = self._leds.get(cmd.id)
        if rt is None:
            # Try by address+pin fallback
            for led in self._leds.values():
                if led.cfg.device_addr == cmd.device_addr and led.cfg.pin == cmd.pin:
                    rt = led
                    break
        if rt is None:
            logger.warning(
                "LedCommand for unknown led id=%s addr=%s pin=%s",
                cmd.id,
                hex(cmd.device_addr),
                cmd.pin,
            )
            return

        dev = self._devices.get(rt.cfg.device_addr)
        if dev is None:
            logger.error("No device at %s for led %s", hex(rt.cfg.device_addr), cmd.id)
            return

        # Cancel previous blink
        if cmd.id in self._blink_tasks:
            self._blink_tasks[cmd.id].cancel()
            try:
                await self._blink_tasks[cmd.id]
            except asyncio.CancelledError:
                pass
            del self._blink_tasks[cmd.id]

        if cmd.blink_ms and cmd.blink_ms > 0:
            # Start blink task
            task = asyncio.create_task(self._blink_led(rt, cmd), name=f"blink-{cmd.id}")
            self._blink_tasks[cmd.id] = task
        else:
            eff = cmd.state ^ rt.cfg.invert
            await self._run_in_executor(dev.write_pin, rt.cfg.pin, eff)
            rt.state = eff
            await self.bus.publish(
                LedState(
                    id=rt.cfg.id,
                    device_addr=rt.cfg.device_addr,
                    pin=rt.cfg.pin,
                    state=eff,
                )
            )

    async def _blink_led(self, rt: LedRuntime, cmd: LedCommand) -> None:
        """_blink_led."""
        assert cmd.blink_ms is not None and cmd.blink_ms > 0
        dev = self._devices[rt.cfg.device_addr]
        period = cmd.blink_ms / 1000.0 / 2.0  # half period
        state = cmd.state
        try:
            while True:
                eff = state ^ rt.cfg.invert
                await self._run_in_executor(dev.write_pin, rt.cfg.pin, eff)
                rt.state = eff
                await self.bus.publish(
                    LedState(
                        id=rt.cfg.id,
                        device_addr=rt.cfg.device_addr,
                        pin=rt.cfg.pin,
                        state=eff,
                    )
                )
                await asyncio.sleep(period)
                state = not state
        except asyncio.CancelledError:
            # Ensure final state is cmd.state (non-blinking)
            eff = cmd.state ^ rt.cfg.invert
            try:
                await self._run_in_executor(dev.write_pin, rt.cfg.pin, eff)
                rt.state = eff
            except Exception:
                pass
            raise

    # -- Poll loop (hardware → main) -------------------------------------------

    async def _poll_loop(self) -> None:
        """_poll_loop."""
        interval = self.cfg.poll_interval_ms / 1000.0
        while self._running:
            start = time.monotonic()
            try:
                await self._poll_once(start)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.exception("Poll error: %s", exc)
            # Sleep remaining interval
            elapsed = time.monotonic() - start
            sleep_for = max(0.0, interval - elapsed)
            await asyncio.sleep(sleep_for)

    async def _poll_once(self, now: float) -> None:
        """_poll_once."""
        # Read all devices in parallel (per device lock already in _run_in_executor)
        # Snapshot GPIOs
        gpios: dict[int, int] = {}
        for addr, dev in self._devices.items():
            val = await self._run_in_executor(dev.read_gpio)
            gpios[addr] = val

        # Buttons
        for bc in self.cfg.buttons:
            gpio = gpios.get(bc.device_addr)
            if gpio is None:
                continue
            raw_high = bool((gpio >> bc.pin) & 1)
            state = self._button_states[bc.id]
            btn_evt = state.update(raw_high, now)
            if btn_evt:
                await self.bus.publish(btn_evt)

        # Encoders (A/B + optional button)
        for ec in self.cfg.encoders:
            gpio = gpios.get(ec.device_addr)
            if gpio is None:
                continue
            a_high = bool((gpio >> ec.pin_a) & 1)
            b_high = bool((gpio >> ec.pin_b) & 1)
            enc_state = self._encoder_states[ec.id]
            enc_evt = enc_state.update(a_high, b_high, now)
            if enc_evt:
                await self.bus.publish(enc_evt)

            if ec.pin_button is not None:
                raw_high = bool((gpio >> ec.pin_button) & 1)
                btn_state = self._encoder_button_states[ec.id]
                bev = btn_state.update(raw_high, now)
                if bev:
                    # Publish as EncoderButtonEvent
                    await self.bus.publish(
                        EncoderButtonEvent(
                            id=ec.id,
                            device_addr=ec.device_addr,
                            pin=ec.pin_button,
                            pressed=bev.pressed,
                            timestamp=now,
                        )
                    )

    # -- Test helpers ------------------------------------------------------------

    async def set_led(self, led_id: str, state: bool) -> None:
        """Direct LED control (for tests/main without bus)."""
        cfg = next((c for c in self.cfg.leds if c.id == led_id), None)
        if cfg is None:
            raise ValueError(f"unknown led {led_id}")
        await self.bus.publish(
            LedCommand(id=led_id, device_addr=cfg.device_addr, pin=cfg.pin, state=state)
        )
