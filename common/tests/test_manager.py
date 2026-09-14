"""Tests for HardwareManager with FakeSMBus – no I2C hardware."""

import asyncio

import pytest

from takpi_common.bus import EventBus
from takpi_common.mcp23017.driver import FakeSMBus
from takpi_common.mcp23017.io import (
    ButtonConfig,
    ButtonEvent,
    EncoderConfig,
    EncoderEvent,
    LedCommand,
    LedConfig,
)
from takpi_common.mcp23017.manager import HardwareConfig, HardwareManager


@pytest.mark.asyncio
async def test_button_debounce_and_event() -> None:
    bus = EventBus()
    fake = FakeSMBus()
    cfg = HardwareConfig(
        devices=[0x20],
        buttons=[ButtonConfig(id="btn1", device_addr=0x20, pin=0, debounce_ms=20)],
        poll_interval_ms=5,
        smbus=fake,
    )
    # Initially high (released, pull-up)
    fake.set_input(0x20, 0, True)
    mgr = HardwareManager(cfg, bus)
    await mgr.start()
    events: list[ButtonEvent] = []
    bus.subscribe(ButtonEvent, lambda e: events.append(e))

    # Press: drive low, wait debounce (20ms) + poll
    fake.set_input(0x20, 0, False)
    await asyncio.sleep(0.05)
    assert any(e.pressed for e in events), events
    events.clear()
    # Release
    fake.set_input(0x20, 0, True)
    await asyncio.sleep(0.05)
    assert any(not e.pressed for e in events)
    await mgr.stop()


@pytest.mark.asyncio
async def test_encoder_quadrature() -> None:
    bus = EventBus()
    fake = FakeSMBus()
    cfg = HardwareConfig(
        devices=[0x20],
        encoders=[EncoderConfig(id="enc1", device_addr=0x20, pin_a=1, pin_b=2)],
        poll_interval_ms=2,
        smbus=fake,
    )
    # Start with 00
    fake.set_input(0x20, 1, False)
    fake.set_input(0x20, 2, False)
    mgr = HardwareManager(cfg, bus)
    await mgr.start()
    events: list[EncoderEvent] = []
    bus.subscribe(EncoderEvent, lambda e: events.append(e))
    # Simulate CW sequence 00→01→11→10→00 (delta +1 each step)
    seq = [(False, True), (True, True), (True, False), (False, False)]
    for a, b in seq:
        fake.set_input(0x20, 1, a)
        fake.set_input(0x20, 2, b)
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    # Should have emitted at least 3-4 events, net position +4 or similar
    assert len(events) >= 3, events
    # Position should be positive (CW)
    assert events[-1].position > 0
    await mgr.stop()


@pytest.mark.asyncio
async def test_led_command_and_blink() -> None:
    bus = EventBus()
    fake = FakeSMBus()
    cfg = HardwareConfig(
        devices=[0x20],
        leds=[LedConfig(id="led1", device_addr=0x20, pin=8)],
        smbus=fake,
    )
    mgr = HardwareManager(cfg, bus)
    await mgr.start()
    # Steady on
    await bus.publish(LedCommand(id="led1", device_addr=0x20, pin=8, state=True))
    await asyncio.sleep(0.02)
    assert fake.get_olat(0x20, 8) is True
    # Off
    await bus.publish(LedCommand(id="led1", device_addr=0x20, pin=8, state=False))
    await asyncio.sleep(0.02)
    assert fake.get_olat(0x20, 8) is False
    # Blink
    await bus.publish(
        LedCommand(id="led1", device_addr=0x20, pin=8, state=True, blink_ms=20)
    )
    await asyncio.sleep(0.05)
    # Should have toggled at least once – check blink task exists
    assert "led1" in mgr._blink_tasks  # type: ignore[attr-defined]
    await mgr.stop()
    # After stop, blink cancelled


@pytest.mark.asyncio
async def test_daisy_chain_multi_device() -> None:
    bus = EventBus()
    fake = FakeSMBus()
    cfg = HardwareConfig(
        devices=[0x20, 0x21],
        buttons=[ButtonConfig(id="btn0", device_addr=0x20, pin=0, debounce_ms=20)],
        leds=[LedConfig(id="led1", device_addr=0x21, pin=5)],
        poll_interval_ms=5,
        smbus=fake,
    )
    fake.set_input(0x20, 0, True)
    mgr = HardwareManager(cfg, bus)
    await mgr.start()
    await asyncio.sleep(0.03)  # let initial released stabilize
    events: list[ButtonEvent] = []
    bus.subscribe(ButtonEvent, lambda e: events.append(e))
    fake.set_input(0x20, 0, False)
    await asyncio.sleep(0.08)  # debounce 20ms + poll
    assert events and events[0].device_addr == 0x20
    # LED on other device
    await bus.publish(LedCommand(id="led1", device_addr=0x21, pin=5, state=True))
    await asyncio.sleep(0.05)
    assert fake.get_olat(0x21, 5) is True
    assert fake.get_olat(0x20, 5) is False  # isolation
    await mgr.stop()


@pytest.mark.asyncio
async def test_button_triggers_led_via_bus() -> None:
    """Hardware → main → LED bidirectional via bus (no CoT)."""
    bus = EventBus()
    fake = FakeSMBus()
    cfg = HardwareConfig(
        devices=[0x20],
        buttons=[ButtonConfig(id="btn1", device_addr=0x20, pin=0, debounce_ms=20)],
        leds=[LedConfig(id="led1", device_addr=0x20, pin=8)],
        smbus=fake,
    )
    fake.set_input(0x20, 0, True)
    mgr = HardwareManager(cfg, bus)
    await mgr.start()
    await asyncio.sleep(0.03)

    # Main logic: button press → LED on
    async def on_button(evt: ButtonEvent) -> None:
        await bus.publish(
            LedCommand(id="led1", device_addr=0x20, pin=8, state=evt.pressed)
        )

    bus.subscribe(ButtonEvent, on_button)
    fake.set_input(0x20, 0, False)
    await asyncio.sleep(0.08)
    assert fake.get_olat(0x20, 8) is True
    fake.set_input(0x20, 0, True)
    await asyncio.sleep(0.08)
    assert fake.get_olat(0x20, 8) is False
    await mgr.stop()
