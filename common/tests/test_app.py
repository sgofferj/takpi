"""Tests for TakPiApp – integration of hardware + CotBus via EventBus."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from takpi_common.bus import EventBus
from takpi_common.cot_bus import CotReceived, CotSend
from takpi_common.mcp23017.driver import FakeSMBus
from takpi_common.mcp23017.io import ButtonConfig, LedConfig
from takpi_common.mcp23017.manager import HardwareConfig
from takpi_common.app import CotConfig, TakPiApp


class FakeCotEvent:
    def __init__(self, cot_type: str = "a-f-G-U-C") -> None:
        self.cot_type = cot_type
        self.uid = "test-uid"
        self.callsign = "TEST"
        self.team = "Cyan"

    def is_emergency(self) -> bool:
        return self.cot_type.startswith("b-a-o-tif")

    def is_chat(self) -> bool:
        return self.cot_type.startswith("b-t-f")


@pytest.mark.asyncio
async def test_app_button_triggers_cot_and_led() -> None:
    bus = EventBus()
    fake = FakeSMBus()
    hw_cfg = HardwareConfig(
        devices=[0x20],
        buttons=[ButtonConfig(id="btn_emergency", device_addr=0x20, pin=0)],
        leds=[LedConfig(id="led_alert", device_addr=0x20, pin=0)],
        smbus=fake,
    )
    fake.set_input(0x20, 0, True)
    # No CotConfig → hardware only, no real stream
    app = TakPiApp(hardware_cfg=hw_cfg, bus=bus)
    await app.start()

    # Capture CotSend
    cots: list[CotSend] = []
    bus.subscribe(CotSend, lambda e: cots.append(e))

    # Press emergency button
    fake.set_input(0x20, 0, False)
    await asyncio.sleep(0.08)  # debounce + handler
    assert len(cots) >= 1
    # LED should be on after handler
    await asyncio.sleep(0.02)
    assert fake.get_olat(0x20, 0) is True

    # Release
    fake.set_input(0x20, 0, True)
    await asyncio.sleep(0.05)
    await app.stop()


@pytest.mark.asyncio
async def test_app_cot_triggers_led() -> None:
    bus = EventBus()
    fake = FakeSMBus()
    hw_cfg = HardwareConfig(
        devices=[0x20, 0x21],
        leds=[
            LedConfig(id="led_alert", device_addr=0x20, pin=0),
            LedConfig(id="led_chat", device_addr=0x20, pin=1),
        ],
        smbus=fake,
    )
    app = TakPiApp(hardware_cfg=hw_cfg, bus=bus)
    await app.start()
    await asyncio.sleep(0.02)

    # Inject CotReceived directly on bus (as if CotBus received it)
    await bus.publish(CotReceived(cot=FakeCotEvent(cot_type="b-a-o-tif")))
    await asyncio.sleep(0.08)
    # Emergency should blink led_alert
    # Check blink task started
    assert "led_alert" in app.hardware._blink_tasks  # type: ignore[union-attr]

    await bus.publish(CotReceived(cot=FakeCotEvent(cot_type="b-t-f")))
    await asyncio.sleep(0.05)
    assert fake.get_olat(0x20, 1) is True

    await app.stop()


@pytest.mark.asyncio
async def test_app_with_fake_stream() -> None:
    bus = EventBus()
    fake = FakeSMBus()
    hw_cfg = HardwareConfig(devices=[0x20], smbus=fake)
    # Mock CotStream
    fake_stream = MagicMock()
    fake_stream.__aiter__ = lambda _: (_ for _ in [])
    fake_stream.send = AsyncMock()
    fake_stream.close = AsyncMock()
    # Patch CotStream.connect to return fake_stream
    from unittest.mock import patch

    with patch("takstream.CotStream.connect", new=AsyncMock(return_value=fake_stream)):
        cot_cfg = CotConfig(
            host="tak.example.com",
            port=8089,
            callsign="PI-TEST",
            team="Cyan",
            role="Team Member",
        )
        app = TakPiApp(hardware_cfg=hw_cfg, cot_cfg=cot_cfg, bus=bus)
        await app.start()
        assert app.cot_bus is not None
        assert app._stream is fake_stream
        await app.stop()
        assert fake_stream.close.called
