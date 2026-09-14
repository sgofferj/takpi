"""Tests for EventBus – hardware ↔ main ↔ CoT."""

import asyncio

import pytest

from takpi_common.bus import EventBus
from takpi_common.mcp23017.io import ButtonEvent, LedCommand


@pytest.mark.asyncio
async def test_publish_subscribe() -> None:
    bus = EventBus()
    received: list[ButtonEvent] = []

    def handler(evt: ButtonEvent) -> None:
        received.append(evt)

    bus.subscribe(ButtonEvent, handler)
    evt = ButtonEvent(id="btn1", device_addr=0x20, pin=0, pressed=True)
    await bus.publish(evt)
    assert received == [evt]
    assert bus.subscribed(ButtonEvent) == 1


@pytest.mark.asyncio
async def test_wildcard_and_unsubscribe() -> None:
    bus = EventBus()
    all_events: list[object] = []

    unsub = bus.subscribe(object, lambda e: all_events.append(e))
    await bus.publish(ButtonEvent(id="b", device_addr=0x20, pin=0, pressed=True))
    assert len(all_events) == 1
    unsub()
    await bus.publish(ButtonEvent(id="b", device_addr=0x20, pin=0, pressed=False))
    assert len(all_events) == 1  # not called after unsubscribe


@pytest.mark.asyncio
async def test_async_handler_and_exception_isolation() -> None:
    bus = EventBus()
    calls: list[str] = []

    async def good(evt: ButtonEvent) -> None:
        calls.append("good")

    def bad(evt: ButtonEvent) -> None:
        calls.append("bad")
        raise RuntimeError("handler fail")

    async def good2(evt: ButtonEvent) -> None:
        calls.append("good2")

    bus.subscribe(ButtonEvent, good)
    bus.subscribe(ButtonEvent, bad)
    bus.subscribe(ButtonEvent, good2)
    await bus.publish(ButtonEvent(id="x", device_addr=0x20, pin=0, pressed=True))
    # All three called, bad exception logged not propagated
    assert calls == ["good", "bad", "good2"]


@pytest.mark.asyncio
async def test_wait_for() -> None:
    bus = EventBus()

    async def publisher() -> None:
        await asyncio.sleep(0.01)
        await bus.publish(LedCommand(id="led1", device_addr=0x20, pin=0, state=True))

    asyncio.create_task(publisher())
    evt = await bus.wait_for(LedCommand, timeout=1.0)
    assert evt.id == "led1"
    assert evt.state is True


@pytest.mark.asyncio
async def test_bidirectional_hardware_main() -> None:
    """Button → main → LedCommand flow via bus."""
    bus = EventBus()

    # Main subscribes to ButtonEvent and publishes LedCommand
    async def on_button(evt: ButtonEvent) -> None:
        await bus.publish(
            LedCommand(id="led1", device_addr=0x20, pin=8, state=evt.pressed)
        )

    bus.subscribe(ButtonEvent, on_button)

    led_states: list[LedCommand] = []
    bus.subscribe(LedCommand, lambda e: led_states.append(e))

    await bus.publish(ButtonEvent(id="btn1", device_addr=0x20, pin=0, pressed=True))
    assert len(led_states) == 1
    assert led_states[0].state is True
    await bus.publish(ButtonEvent(id="btn1", device_addr=0x20, pin=0, pressed=False))
    assert led_states[1].state is False
