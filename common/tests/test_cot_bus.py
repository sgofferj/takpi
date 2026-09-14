"""Tests for CotBus – bridges takstream CotStream ↔ EventBus."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from takpi_common.bus import EventBus
from takpi_common.cot_bus import CotBus, CotReceived, CotSend


class FakeCotEvent:
    def __init__(self, cot_type: str = "a-f-G-U-C", uid: str = "test-uid") -> None:
        self.cot_type = cot_type
        self.uid = uid
        self.callsign = "TEST"
        self.team = "Cyan"

    def is_emergency(self) -> bool:
        return self.cot_type.startswith("b-a-o-tif")

    def is_chat(self) -> bool:
        return self.cot_type.startswith("b-t-f")


@pytest.mark.asyncio
async def test_cot_received_published() -> None:
    bus = EventBus()

    # Fake stream that yields one event then closes
    async def fake_aiter() -> FakeCotEvent:
        yield FakeCotEvent(cot_type="a-f-G-U-C")

    fake_stream = MagicMock()
    fake_stream.__aiter__ = lambda _: fake_aiter()  # type: ignore[assignment]
    fake_stream.send = AsyncMock()

    cot_bus = CotBus(bus, fake_stream)
    await cot_bus.start()
    received: list[CotReceived] = []
    bus.subscribe(CotReceived, lambda e: received.append(e))
    # Wait for recv loop to publish
    await asyncio.sleep(0.05)
    assert len(received) == 1
    assert received[0].cot.cot_type == "a-f-G-U-C"  # type: ignore[union-attr]
    await cot_bus.stop()


@pytest.mark.asyncio
async def test_cot_send_via_bus() -> None:
    bus = EventBus()
    fake_stream = MagicMock()
    fake_stream.__aiter__ = lambda _: (_ for _ in [])  # empty
    fake_stream.send = AsyncMock()
    fake_stream.receive = AsyncMock(return_value=None)

    cot_bus = CotBus(bus, fake_stream)
    await cot_bus.start()
    cot = FakeCotEvent(cot_type="b-m-p-s-m")
    await bus.publish(CotSend(cot=cot))
    await asyncio.sleep(0.02)
    assert fake_stream.send.called
    assert fake_stream.send.call_args[0][0] == cot
    await cot_bus.stop()


@pytest.mark.asyncio
async def test_cot_send_helper() -> None:
    bus = EventBus()
    fake_stream = MagicMock()
    fake_stream.__aiter__ = lambda _: (_ for _ in [])
    fake_stream.send = AsyncMock()
    cot_bus = CotBus(bus, fake_stream)
    await cot_bus.start()
    cot = FakeCotEvent()
    await cot_bus.send(cot)
    await asyncio.sleep(0.02)
    assert fake_stream.send.called
    await cot_bus.stop()


@pytest.mark.asyncio
async def test_bidirectional_cot_led() -> None:
    """CotReceived → LedCommand via bus (main logic) – no hardware."""
    from takpi_common.mcp23017.io import LedCommand

    bus = EventBus()
    fake_stream = MagicMock()
    fake_stream.__aiter__ = lambda _: (_ for _ in [])
    fake_stream.send = AsyncMock()
    cot_bus = CotBus(bus, fake_stream)
    await cot_bus.start()

    # Main logic: emergency CoT → blink LED
    async def on_cot(evt: CotReceived) -> None:
        cot = evt.cot
        if hasattr(cot, "is_emergency") and cot.is_emergency():  # type: ignore[call-arg]
            await bus.publish(
                LedCommand(
                    id="led_alert", device_addr=0x20, pin=0, state=True, blink_ms=500
                )
            )

    bus.subscribe(CotReceived, on_cot)
    led_cmds: list[LedCommand] = []
    bus.subscribe(LedCommand, lambda e: led_cmds.append(e))

    # Simulate inbound CoT from stream
    await bus.publish(CotReceived(cot=FakeCotEvent(cot_type="b-a-o-tif")))
    await asyncio.sleep(0.02)
    assert any(c.id == "led_alert" and c.blink_ms == 500 for c in led_cmds)
    await cot_bus.stop()
