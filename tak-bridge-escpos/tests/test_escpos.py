"""Tests for async ESC/POS printer driver – softserial GPIO + EventBus, no hardware."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from tak_bridge_escpos.escpos import EscPosPrinter, FakeSerial, PrintRequest


@pytest.mark.asyncio
async def test_fake_serial_basic() -> None:
    fake = FakeSerial()
    printer = EscPosPrinter(serial_instance=fake)
    await printer.connect()
    assert printer.is_open
    await printer.init_printer()
    await printer.disconnect()
    assert not printer.is_open
    assert b"\x1b\x40" in fake.get_all()


@pytest.mark.asyncio
async def test_softserial_fake_via_injection() -> None:
    fake_soft = MagicMock()
    fake_soft.is_open = True
    fake_soft.write = MagicMock(return_value=2)
    fake_soft.close = MagicMock()
    # Inject softserial instance (simulates pigpio FakePigpio)
    printer = EscPosPrinter(
        tx_pin=27, rx_pin=22, baudrate=9600, softserial_instance=fake_soft
    )
    assert printer.is_softserial
    await printer.connect()
    await printer.print_text("hi")
    assert fake_soft.write.called
    await printer.disconnect()


@pytest.mark.asyncio
async def test_convenience_fat_double() -> None:
    fake = FakeSerial()
    printer = EscPosPrinter(serial_instance=fake)
    await printer.connect()
    fake.clear()
    await printer.fat("911 ALARM")
    data = fake.get_all()
    # Fat = bold on + GS ! 0x11
    assert b"\x1bE\x01" in data
    assert b"\x1d!\x11" in data
    assert b"911 ALARM" in data
    # Should reset after
    assert b"\x1bE\x00" in data
    assert b"\x1d!\x00" in data

    fake.clear()
    await printer.double_height("hi")
    assert b"\x1d!\x01" in fake.get_all()

    fake.clear()
    await printer.double_width("hi")
    assert b"\x1d!\x10" in fake.get_all()

    fake.clear()
    await printer.bold("hi")
    assert b"\x1bE\x01" in fake.get_all()
    await printer.disconnect()


@pytest.mark.asyncio
async def test_print_text_styles_and_align() -> None:
    fake = FakeSerial()
    printer = EscPosPrinter(serial_instance=fake, baudrate=19200)
    await printer.connect()
    assert printer.baudrate == 19200
    fake.clear()
    await printer.print_text("centered", align="center", bold=True, underline=True)
    data = fake.get_all()
    assert b"\x1b" + b"a\x01" in data  # center
    assert b"\x1bE\x01" in data
    assert b"\x1b-\x01" in data
    assert b"centered" in data
    await printer.disconnect()


@pytest.mark.asyncio
async def test_print_alarm_contains_callsign_and_location() -> None:
    fake = FakeSerial()
    printer = EscPosPrinter(serial_instance=fake)
    await printer.connect()
    fake.clear()
    await printer.print_alarm(
        callsign="ALPHA",
        lat=60.12345,
        lon=24.98765,
        location="Helsinki",
        when=datetime(2026, 9, 7, 12, 34, 56, tzinfo=timezone.utc),
        remarks="car crash",
        cot_type="b-a-o-tif",
    )
    data = fake.get_all().decode("cp437", errors="replace")
    assert "911 ALARM" in data
    assert "ALPHA" in data
    assert "60.12345" in data or "60.1234" in data
    assert "Helsinki" in data
    assert "b-a-o-tif" in data
    assert "car crash" in data
    await printer.disconnect()


@pytest.mark.asyncio
async def test_print_request_via_bus() -> None:
    """Main → printer via EventBus PrintRequest (fat, cut)."""
    from takpi_common.bus import EventBus

    fake = FakeSerial()
    printer = EscPosPrinter(serial_instance=fake)
    await printer.connect()
    bus = EventBus()
    bus.subscribe(PrintRequest, printer.handle_print_request)

    fake.clear()
    await bus.publish(
        PrintRequest(
            text="Hello", fat=True, align="center", cut_after=True, feed_lines=2
        )
    )
    # Allow handler to run
    await asyncio.sleep(0.05)
    data = fake.get_all()
    assert b"Hello" in data
    assert b"\x1d!\x11" in data  # fat
    assert b"\x1dV\x00" in data  # cut
    await printer.disconnect()


@pytest.mark.asyncio
async def test_cot_911_auto_print_via_bus() -> None:
    """CotReceived (emergency) → 911 alarm print via main handler."""
    from takpi_common.bus import EventBus
    from takpi_common.cot_bus import CotReceived

    fake = FakeSerial()
    printer = EscPosPrinter(serial_instance=fake)
    await printer.connect()
    bus = EventBus()

    # Main's cot → print handler as in __main__.py
    async def on_cot(evt):  # type: ignore[no-untyped-def]
        cot = evt.cot
        is_em = cot.is_emergency() if hasattr(cot, "is_emergency") else False
        if is_em:
            callsign = getattr(cot, "callsign", "unknown")
            lat = getattr(cot, "lat", None)
            lon = getattr(cot, "lon", None)
            await printer.print_alarm(
                callsign=str(callsign), lat=lat, lon=lon, location=None
            )

    bus.subscribe(CotReceived, on_cot)

    class FakeCot:
        cot_type = "b-a-o-tif"
        uid = "em-123"
        callsign = "ALPHA"
        lat = 60.1
        lon = 24.8

        def is_emergency(self) -> bool:
            return True

    fake.clear()
    await bus.publish(CotReceived(cot=FakeCot()))
    await asyncio.sleep(0.05)
    data = fake.get_all().decode("cp437", errors="replace")
    assert "911 ALARM" in data
    assert "ALPHA" in data
    assert "60.1" in data
    await printer.disconnect()


@pytest.mark.asyncio
async def test_configurable_baudrate_and_gpio_pins() -> None:
    # Hardware port with custom baudrate
    fake = FakeSerial()
    p1 = EscPosPrinter(port="/dev/ttyUSB0", baudrate=115200, serial_instance=fake)
    assert p1.baudrate == 115200
    assert p1.port == "/dev/ttyUSB0"
    assert not p1.is_softserial

    # Softserial with BCM pins
    fake_soft = MagicMock()
    fake_soft.is_open = True
    fake_soft.write = MagicMock(return_value=1)
    fake_soft.close = MagicMock()
    p2 = EscPosPrinter(
        tx_pin=27, rx_pin=22, baudrate=19200, softserial_instance=fake_soft
    )
    assert p2.tx_pin == 27
    assert p2.rx_pin == 22
    assert p2.baudrate == 19200
    assert p2.is_softserial


@pytest.mark.asyncio
async def test_header_only() -> None:
    # Invalid config should raise
    with __import__("pytest").raises(ValueError):
        EscPosPrinter()  # no port nor tx_pin nor instance
