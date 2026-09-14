#!/usr/bin/env python3
"""
Panel demo – MCP23017 buttons/encoders ↔ EventBus ↔ CotStream (takstream) ↔ LEDs.

Wiring (daisy-chain on I2C-1):
  MCP #0 @0x20 (A0=A1=A2=GND) – inputs: GPA0 button, GPB0/1 encoder A/B, GPB2 encoder button
  MCP #1 @0x21 (A0=VCC) – outputs: GPA0 alert LED, GPA1 chat LED

This demo runs with FakeSMBus (no hardware) and a fake CotStream,
but the same code works on real Pi with `smbus=None` (real /dev/i2c-1)
and real CotStream.connect(host,port,cert,...).

Bidirectional flow demonstrated:
  1. Button press → main → CotSend (emergency) + LedCommand
  2. Encoder rotate → main → LedCommand
  3. CotReceived (emergency) → main → LedCommand blink
  4. CotReceived (chat) → main → LedCommand

Run:
  poetry run --directory common python examples/panel_demo.py
  poetry run --directory common python examples/panel_demo.py --with-cot  # needs TAK_HOST etc via .env
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure common/src is importable when run as script
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from takpi_common.bus import EventBus
from takpi_common.mcp23017.driver import FakeSMBus
from takpi_common.mcp23017.io import ButtonConfig, EncoderConfig, LedConfig
from takpi_common.mcp23017.manager import HardwareConfig, HardwareManager
from takpi_common.cot_bus import CotBus, CotReceived, CotSend

try:
    from takstream import CotEvent, CotStream  # type: ignore[import-not-found]
except ImportError:
    CotEvent = None  # type: ignore[assignment]
    CotStream = None  # type: ignore[assignment]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def run_fake_demo() -> None:
    logger.info("=== Fake demo (no hardware, no TAK server) ===")
    bus = EventBus()
    fake = FakeSMBus()

    hw_cfg = HardwareConfig(
        devices=[0x20, 0x21],
        buttons=[ButtonConfig(id="btn_emergency", device_addr=0x20, pin=0)],
        encoders=[
            EncoderConfig(id="enc1", device_addr=0x20, pin_a=1, pin_b=2, pin_button=3)
        ],
        leds=[
            LedConfig(id="led_alert", device_addr=0x21, pin=0),
            LedConfig(id="led_chat", device_addr=0x21, pin=1),
        ],
        poll_interval_ms=5,
        smbus=fake,
    )
    # HardwareManager publishes ButtonEvent etc. on bus
    async with HardwareManager(hw_cfg, bus):
        # Fake CotStream for CotBus (async iterable, no inbound)
        from unittest.mock import AsyncMock, MagicMock

        async def _empty_aiter():  # type: ignore[no-untyped-def]
            if False:
                yield
            return

        fake_stream = MagicMock()
        fake_stream.send = AsyncMock()
        fake_stream.__aiter__ = (
            lambda _: _empty_aiter()
        )  # async generator, yields nothing

        cot_bus = CotBus(bus, fake_stream)
        await cot_bus.start()

        # Main handlers (same as TakPiApp but inline for demo)
        async def on_button(evt):  # type: ignore[no-untyped-def]
            logger.info(
                "Button %s %s", evt.id, "pressed" if evt.pressed else "released"
            )
            if evt.id == "btn_emergency" and evt.pressed:
                cot = (
                    CotEvent.emergency_alert(lat=60.0, lon=24.0, callsign="PI-PANEL")
                    if CotEvent
                    else None
                )
                if cot:
                    await bus.publish(CotSend(cot=cot))
                    logger.info("Sent emergency CoT via CotBus (mocked)")
                await bus.publish(
                    __import__(
                        "takpi_common.mcp23017.io", fromlist=["LedCommand"]
                    ).LedCommand(id="led_alert", device_addr=0x21, pin=0, state=True)
                )

        async def on_cot(evt: CotReceived):
            cot = evt.cot
            ctype = getattr(cot, "cot_type", "?")
            logger.info("CoT received %s -> LED", ctype)
            if hasattr(cot, "is_emergency") and cot.is_emergency():
                from takpi_common.mcp23017.io import LedCommand

                await bus.publish(
                    LedCommand(
                        id="led_alert",
                        device_addr=0x21,
                        pin=0,
                        state=True,
                        blink_ms=500,
                    )
                )
            if hasattr(cot, "is_chat") and cot.is_chat():
                from takpi_common.mcp23017.io import LedCommand

                await bus.publish(
                    LedCommand(id="led_chat", device_addr=0x21, pin=1, state=True)
                )

        bus.subscribe(
            __import__(
                "takpi_common.mcp23017.io", fromlist=["ButtonEvent"]
            ).ButtonEvent,
            on_button,
        )
        bus.subscribe(CotReceived, on_cot)

        # Simulate hardware button press (active-low)
        logger.info("Simulating button press on GPA0 (0x20 pin0 low)")
        fake.set_input(0x20, 0, False)
        await asyncio.sleep(0.08)  # debounce
        logger.info("OLAT led_alert after press: %s", fake.get_olat(0x21, 0))
        fake.set_input(0x20, 0, True)
        await asyncio.sleep(0.05)
        logger.info("Simulating encoder CW turn (A/B sequence)")
        for a, b in [(False, True), (True, True), (True, False), (False, False)]:
            fake.set_input(0x20, 1, a)
            fake.set_input(0x20, 2, b)
            await asyncio.sleep(0.01)

        # Simulate inbound CoT (emergency) → LED blink
        logger.info("Simulating inbound emergency CoT → LED blink")
        if CotEvent:
            cot = CotEvent.emergency_alert(lat=60, lon=24, callsign="INCOMING")
            await bus.publish(CotReceived(cot=cot))
            await asyncio.sleep(0.1)
            logger.info("OLAT led_alert blink task exists: %s", fake.get_olat(0x21, 0))

        # Simulate inbound chat → chat LED
        if CotEvent:
            from takstream import CotEvent as CE

            chat = CE.chat_message(
                "hello", sender_callsign="ALPHA", sender_uid="UID123"
            )
            await bus.publish(CotReceived(cot=chat))
            await asyncio.sleep(0.05)
            logger.info("OLAT led_chat after chat: %s", fake.get_olat(0x21, 1))

        await cot_bus.stop()
    logger.info("Fake demo done")


async def run_with_cot() -> None:
    # Real TAK server demo – reads .env for TAK_HOST etc.
    from takpi_common.config import load_env, get_str, get_int

    load_env()
    host = get_str("TAK_HOST")
    port = get_int("TAK_PORT", 8089)
    if not host:
        logger.error("TAK_HOST not set in .env – cannot run --with-cot")
        return

    bus = EventBus()
    hw_cfg = HardwareConfig(
        devices=[0x20, 0x21],
        buttons=[ButtonConfig(id="btn_emergency", device_addr=0x20, pin=0)],
        leds=[LedConfig(id="led_alert", device_addr=0x21, pin=0)],
        poll_interval_ms=5,
    )
    from takpi_common.app import CotConfig, TakPiApp

    cot_cfg = CotConfig(
        host=host,
        port=port,
        cert=get_str("TAK_CERT") or None,
        key=get_str("TAK_KEY") or None,
        ca=get_str("TAK_CA") or None,
        callsign=get_str("TAK_CALLSIGN", "PI-PANEL"),
        team=get_str("TAK_TEAM", "Cyan"),
        role=get_str("TAK_ROLE", "Team Member"),
    )
    async with TakPiApp(hardware_cfg=hw_cfg, cot_cfg=cot_cfg, bus=bus):
        logger.info(
            "Connected to TAK %s:%d, hardware on I2C-1, press btn_emergency or send CoTs to see LEDs",
            host,
            port,
        )
        await asyncio.sleep(60)


if __name__ == "__main__":
    if "--with-cot" in sys.argv:
        asyncio.run(run_with_cot())
    else:
        asyncio.run(run_fake_demo())
