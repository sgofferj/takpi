"""
Async ESC/POS thermal printer driver for Raspberry Pi – softserial GPIO, configurable baudrate.

Supports:
  - Hardware serial (pyserial) via ``port`` (e.g. /dev/serial0, /dev/ttyUSB0, /dev/ttyAMA0)
  - Softserial (bit-bang) via ``tx_pin``/``rx_pin`` BCM numbers + ``baudrate`` using pigpio (fallback to FakeSerial for CI)
  - Configurable baudrate (9600, 19200, 38400, 57600, 115200 printer-dependent)
  - Convenience styles: **fat** (bold+double), double-height/width, underline, align, etc.
  - 911 alarm helper for main process: ``print_alarm(callsign, lat, lon, location)``

Protocol: ESC/POS (Epson) – ESC @, ESC E, GS !, ESC a, etc.
No hard dependency on ``python-escpos`` – minimal implementation for async + softserial + EventBus.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ESC/POS command bytes
# ---------------------------------------------------------------------------

ESC = b"\x1b"
GS = b"\x1d"

CMD_INIT = ESC + b"\x40"  # ESC @
CMD_CUT_FULL = GS + b"V\x00"  # GS V 0
CMD_CUT_PARTIAL = GS + b"V\x01"


def _feed_bytes(n: int) -> bytes:
    return ESC + b"d" + bytes([n & 0xFF])


# Text style
CMD_BOLD_ON = ESC + b"E\x01"
CMD_BOLD_OFF = ESC + b"E\x00"
CMD_UNDERLINE_ON = ESC + b"-\x01"
CMD_UNDERLINE_OFF = ESC + b"-\x00"
CMD_DOUBLE_STRIKE_ON = ESC + b"G\x01"
CMD_DOUBLE_STRIKE_OFF = ESC + b"G\x00"

# Character size GS ! n – bits 0-2 height (0=normal,1=double), 4-6 width
SIZE_NORMAL = 0x00
SIZE_DOUBLE_HEIGHT = 0x01
SIZE_DOUBLE_WIDTH = 0x10
SIZE_FAT = 0x11  # double width + double height


def _size_cmd(n: int) -> bytes:
    return GS + b"!" + bytes([n & 0xFF])


ALIGN_LEFT = 0
ALIGN_CENTER = 1
ALIGN_RIGHT = 2

FONT_A = 0
FONT_B = 1


# ---------------------------------------------------------------------------
# EventBus integration – main process → printer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrintRequest:
    """Main → printer via EventBus. ``text`` is raw; ``raw_bytes`` for pre-formatted ESC/POS."""

    text: str | None = None
    raw_bytes: bytes | None = None
    bold: bool = False
    double_width: bool = False
    double_height: bool = False
    fat: bool = False  # fat = bold + double
    underline: bool = False
    align: str = "left"  # left, center, right
    cut_after: bool = False
    feed_lines: int = 0


# ---------------------------------------------------------------------------
# Serial abstractions – hardware (pyserial), soft (pigpio), fake (tests)
# ---------------------------------------------------------------------------


class FakeSerial:
    """In-memory fake for tests – records written bytes, no hardware."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.is_open: bool = True

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def close(self) -> None:
        self.is_open = False

    def flush(self) -> None:
        pass

    def get_all(self) -> bytes:
        return b"".join(self.written)

    def clear(self) -> None:
        self.written.clear()


class FakePigpio:
    """Fake pigpio for CI – mimics bb_serial_write via wave."""

    def __init__(self) -> None:
        self.written: list[tuple[int, bytes]] = []  # (gpio, data)
        self.connected: bool = True

    def set_mode(self, gpio: int, mode: int) -> None:
        pass

    def bb_serial_write(self, gpio: int, data: bytes) -> None:
        self.written.append((gpio, bytes(data)))

    def stop(self) -> None:
        pass


class HardwareSerial:
    """Wrapper around pyserial for hardware UART."""

    def __init__(self, port: str, baudrate: int, timeout: float = 1.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser: Any | None = None

    def open(self) -> None:
        import serial  # type: ignore[import-not-found]

        self._ser = serial.Serial(
            port=self.port, baudrate=self.baudrate, timeout=self.timeout
        )
        # Printers need settle after open (like FI gauges)
        time.sleep(0.5)

    def write(self, data: bytes) -> int:
        if self._ser is None:
            raise RuntimeError("HardwareSerial not open")
        return int(self._ser.write(data))

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and bool(self._ser.is_open)


class SoftSerial:
    """
    Softserial via pigpio bit-bang (BCM pins).

    Uses ``pigpio.pi().wave_add_serial`` + ``wave_send`` for TX.
    For RX, uses ``bb_serial_read_open`` (optional).
    Falls back to FakePigpio if pigpio daemon not running or not installed (tests).
    """

    def __init__(self, tx_pin: int, rx_pin: int | None, baudrate: int) -> None:
        self.tx_pin = tx_pin
        self.rx_pin = rx_pin
        self.baudrate = baudrate
        self._pi: Any | None = None
        self._fake: FakePigpio | None = None
        self._is_fake = False

    def open(self) -> None:
        try:
            import pigpio  # type: ignore[import-not-found]

            pi = pigpio.pi()
            if not pi.connected:
                raise RuntimeError("pigpio daemon not running (sudo pigpiod)")
            self._pi = pi
            pi.set_mode(self.tx_pin, pigpio.OUTPUT)
            if self.rx_pin is not None:
                pi.set_mode(self.rx_pin, pigpio.INPUT)
                pi.bb_serial_read_open(self.rx_pin, self.baudrate, 8)
            time.sleep(0.1)
            logger.info(
                "SoftSerial opened TX BCM%d RX %s @%d via pigpio",
                self.tx_pin,
                str(self.rx_pin),
                self.baudrate,
            )
        except (ImportError, RuntimeError, Exception) as exc:
            logger.warning(
                "SoftSerial pigpio not available (%s), using FakePigpio for TX %d",
                exc,
                self.tx_pin,
            )
            self._fake = FakePigpio()
            self._is_fake = True

    def write(self, data: bytes) -> int:
        if self._is_fake and self._fake is not None:
            self._fake.bb_serial_write(self.tx_pin, data)
            return len(data)
        if self._pi is None:
            raise RuntimeError("SoftSerial not open")
        # Use pigpio wave for TX (blocking but fast at 9600)
        try:
            import pigpio  # type: ignore[import-not-found]

            # pigpio wave method: wave_add_serial + wave_create + wave_send_once
            self._pi.wave_clear()
            self._pi.wave_add_serial(self.tx_pin, self.baudrate, data, 0, 8, 1)
            wid = self._pi.wave_create()
            if wid >= 0:
                self._pi.wave_send_once(wid)
                while self._pi.wave_tx_busy():
                    time.sleep(0.002)
                self._pi.wave_delete(wid)
            else:
                # Fallback to simple bitbang via bb_serial_write if available (pigpio >=1.79)
                try:
                    self._pi.bb_serial_write(self.tx_pin, data)  # type: ignore[attr-defined]
                except AttributeError:
                    raise RuntimeError(f"pigpio wave_create failed {wid}")
            return len(data)
        except Exception as exc:
            logger.exception("SoftSerial write failed: %s", exc)
            raise

    def close(self) -> None:
        if self._pi is not None:
            try:
                if self.rx_pin is not None:
                    self._pi.bb_serial_read_close(self.rx_pin)
                self._pi.stop()
            except Exception:
                pass
            self._pi = None
        self._fake = None

    @property
    def is_open(self) -> bool:
        return self._pi is not None or self._is_fake


# ---------------------------------------------------------------------------
# Async ESC/POS printer
# ---------------------------------------------------------------------------


class EscPosPrinter:
    """
    Async ESC/POS printer driver – hardware serial or softserial GPIO.

    Configurable:
      - ``port`` + ``baudrate`` for hardware (e.g. /dev/serial0, /dev/ttyUSB0, /dev/ttyAMA0)
      - ``tx_pin``/``rx_pin`` (BCM) + ``baudrate`` for softserial via pigpio (e.g. tx_pin=27, rx_pin=22)
      - One of port OR tx_pin must be given (port preferred for hardware, tx_pin for soft)

    Softserial is needed when hardware UART is already used (e.g. chronometer on /dev/serial0)
    and you need a second TTL printer on spare GPIOs.

    Example direct::

        async with EscPosPrinter(tx_pin=27, baudrate=9600) as p:
            await p.fat("911 ALARM")
            await p.print_alarm(callsign="ALPHA", lat=60.1, lon=24.8, location="Helsinki")

    Example via EventBus (main → printer)::

        bus = EventBus()
        printer = EscPosPrinter(port="/dev/serial0", baudrate=19200)
        await printer.connect()
        bus.subscribe(PrintRequest, lambda req: printer.handle_print_request(req))
        await bus.publish(PrintRequest(text="Hello", fat=True, cut_after=True))

    For CI without hardware/pigpio, inject ``serial_instance=FakeSerial()``.
    """

    def __init__(
        self,
        port: str | None = None,
        baudrate: int = 9600,
        tx_pin: int | None = None,
        rx_pin: int | None = None,
        timeout: float = 1.0,
        encoding: str = "cp437",
        serial_instance: Any | None = None,
        softserial_instance: Any | None = None,
    ) -> None:
        if (
            port is None
            and tx_pin is None
            and serial_instance is None
            and softserial_instance is None
        ):
            raise ValueError(
                "Must provide port (hardware) or tx_pin (softserial) or serial_instance"
            )
        if port is not None and tx_pin is not None:
            logger.warning(
                "Both port and tx_pin given – tx_pin (softserial) takes precedence"
            )
        self.port = port
        self.baudrate = baudrate
        self.tx_pin = tx_pin
        self.rx_pin = rx_pin
        self.timeout = timeout
        self.encoding = encoding
        self._serial: Any | None = serial_instance
        self._soft: Any | None = softserial_instance
        self._lock: asyncio.Lock | None = None
        self._is_soft = tx_pin is not None or softserial_instance is not None

    @property
    def is_softserial(self) -> bool:
        return self._is_soft

    @property
    def is_open(self) -> bool:
        if self._is_soft:
            return self._soft is not None and bool(
                getattr(self._soft, "is_open", False)
                or getattr(self._soft, "connected", False)
            )
        return self._serial is not None and bool(
            getattr(self._serial, "is_open", False)
        )

    async def connect(self) -> None:
        """Open serial (hardware or soft). Idempotent."""
        if self.is_open:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        loop = asyncio.get_running_loop()
        if self._is_soft:
            if self._soft is not None:
                # Already injected (Fake)
                return
            soft = SoftSerial(
                tx_pin=self.tx_pin or 0, rx_pin=self.rx_pin, baudrate=self.baudrate
            )
            await loop.run_in_executor(None, soft.open)
            self._soft = soft
            logger.info(
                "EscPos softserial opened BCM TX=%s RX=%s @%d",
                str(self.tx_pin),
                str(self.rx_pin),
                self.baudrate,
            )
        else:
            if self._serial is not None:
                return
            assert self.port is not None
            hw = HardwareSerial(
                port=self.port, baudrate=self.baudrate, timeout=self.timeout
            )
            await loop.run_in_executor(None, hw.open)
            self._serial = hw
            logger.info(
                "EscPos hardware serial opened %s @%d", self.port, self.baudrate
            )
        # Small settle + init
        await asyncio.sleep(0.2)
        await self.init_printer()

    async def disconnect(self) -> None:
        """Close serial."""
        loop = asyncio.get_running_loop()
        if self._is_soft and self._soft is not None:
            soft = self._soft
            await loop.run_in_executor(None, soft.close)
            self._soft = None
        elif self._serial is not None:
            ser = self._serial
            await loop.run_in_executor(None, ser.close)
            self._serial = None

    async def __aenter__(self) -> EscPosPrinter:
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.disconnect()

    # -- low-level write -------------------------------------------------------

    async def _write(self, data: bytes) -> None:
        if not self.is_open:
            raise ConnectionError("EscPosPrinter not connected – call await connect()")
        if self._lock is None:
            self._lock = asyncio.Lock()
        loop = asyncio.get_running_loop()
        async with self._lock:
            target = self._soft if self._is_soft else self._serial
            assert target is not None
            await loop.run_in_executor(None, target.write, data)
            # Printers need tiny gap; 1 ms per 64 bytes is typical for thermal
            await asyncio.sleep(0.005)

    async def _write_text(self, text: str) -> None:
        await self._write(text.encode(self.encoding, errors="replace"))

    # -- ESC/POS primitives ----------------------------------------------------

    async def init_printer(self) -> None:
        """ESC @ – initialize."""
        await self._write(CMD_INIT)

    async def set_bold(self, enabled: bool) -> None:
        await self._write(CMD_BOLD_ON if enabled else CMD_BOLD_OFF)

    async def set_underline(self, enabled: bool) -> None:
        await self._write(CMD_UNDERLINE_ON if enabled else CMD_UNDERLINE_OFF)

    async def set_size(self, width_mul: int = 1, height_mul: int = 1) -> None:
        """GS ! – 1..8 for each axis (1 normal, 2 double)."""
        w = max(1, min(8, width_mul)) - 1
        h = max(1, min(8, height_mul)) - 1
        n = (w << 4) | h
        await self._write(_size_cmd(n))

    async def set_double_width(self, enabled: bool) -> None:
        await self.set_size(width_mul=2 if enabled else 1, height_mul=1)

    async def set_double_height(self, enabled: bool) -> None:
        await self.set_size(width_mul=1, height_mul=2 if enabled else 1)

    async def set_fat(self, enabled: bool) -> None:
        """Fat = bold + double width+height."""
        if enabled:
            await self._write(CMD_BOLD_ON)
            await self._write(_size_cmd(SIZE_FAT))
        else:
            await self._write(CMD_BOLD_OFF)
            await self._write(_size_cmd(SIZE_NORMAL))

    async def set_align(self, align: str) -> None:
        """ESC a n – left/center/right."""
        mapping = {"left": ALIGN_LEFT, "center": ALIGN_CENTER, "right": ALIGN_RIGHT}
        n = mapping.get(align.lower(), ALIGN_LEFT)
        await self._write(ESC + b"a" + bytes([n]))

    async def set_font(self, font: str) -> None:
        """ESC M n – font A/B."""
        n = FONT_B if font.upper() == "B" else FONT_A
        await self._write(ESC + b"M" + bytes([n]))

    async def feed(self, lines: int = 3) -> None:
        if lines > 0:
            await self._write(_feed_bytes(lines))

    async def cut(self, partial: bool = False) -> None:
        await self._write(CMD_CUT_PARTIAL if partial else CMD_CUT_FULL)
        await asyncio.sleep(0.5)  # cutter needs time

    # -- high-level helpers ----------------------------------------------------

    async def print_text(
        self,
        text: str,
        *,
        bold: bool = False,
        double_width: bool = False,
        double_height: bool = False,
        underline: bool = False,
        align: str | None = None,
        fat: bool = False,
        feed_after: int = 0,
    ) -> None:
        """Print text with optional style, auto-resetting.

        ``fat`` overrides bold/double: fat = bold + 2× width + 2× height.
        """
        if fat:
            await self.set_fat(True)
            if align:
                await self.set_align(align)
            await self._write_text(text)
            if not text.endswith("\n"):
                await self._write(b"\n")
            await self.set_fat(False)
        else:
            if bold:
                await self.set_bold(True)
            if double_width or double_height:
                w = 2 if double_width else 1
                h = 2 if double_height else 1
                await self.set_size(w, h)
            if underline:
                await self.set_underline(True)
            if align:
                await self.set_align(align)
            await self._write_text(text)
            if not text.endswith("\n"):
                await self._write(b"\n")
            # Reset
            if bold:
                await self.set_bold(False)
            if double_width or double_height:
                await self.set_size(1, 1)
            if underline:
                await self.set_underline(False)
            if align:
                await self.set_align("left")
        if feed_after:
            await self.feed(feed_after)

    async def fat(self, text: str, align: str = "center") -> None:
        """Convenience: fat (bold+double) centered."""
        await self.print_text(text, fat=True, align=align)

    async def double_height(self, text: str) -> None:
        await self.print_text(text, double_height=True)

    async def double_width(self, text: str) -> None:
        await self.print_text(text, double_width=True)

    async def bold(self, text: str) -> None:
        await self.print_text(text, bold=True)

    async def print_alarm(
        self,
        callsign: str,
        lat: float | None = None,
        lon: float | None = None,
        location: str | None = None,
        when: datetime | None = None,
        remarks: str | None = None,
        cot_type: str | None = None,
    ) -> None:
        """
        Print 911 alarm paper – main process helper.

        Example paper (fat header, details, cut):

          ************
            911 ALARM
          ************
          From: ALPHA
          Loc:  60.1234, 24.9876
                Helsinki, Kamppi
          Time: 2026-09-07 12:34:56 UTC
          Type: b-a-o-tif
          Remarks: car crash
          ----------------
        """
        ts = when or datetime.now(timezone.utc)
        loc_str = location or (
            f"{lat:.4f}, {lon:.4f}"
            if lat is not None and lon is not None
            else "unknown"
        )
        await self.fat("*** 911 ALARM ***")
        await self.print_text(f"From: {callsign}", bold=True)
        if lat is not None and lon is not None:
            await self.print_text(f"Loc:  {lat:.5f}, {lon:.5f}")
        if location:
            await self.print_text(f"      {location}")
        elif loc_str and (lat is None or lon is None):
            await self.print_text(f"Loc:  {loc_str}")
        await self.print_text(f"Time: {ts.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        if cot_type:
            await self.print_text(f"Type: {cot_type}")
        if remarks:
            await self.print_text(f"Remarks: {remarks}")
        await self.print_text("-" * 32)
        await self.feed(2)

    async def print_qr(self, data: str, size: int = 6) -> None:
        """Print QR code (model 2, via GS ( k). Minimal – many printers use different sequences."""
        # This is a generic sequence: GS ( k pL pH cn fn n1 n2 – use common 0x31 commands
        # For portability we use the widely supported variant:
        # Use ESC/POS QR: GS ( k 4 0 31 50 0  ... but we keep simple and fall back to text QR placeholder
        # For real printer, replace with printer-specific sequence.
        # Here we just print the data as text with a QR header.
        await self.print_text("[QR]", bold=True, align="center")
        await self.print_text(data, align="center")
        # TODO: printer-specific GS ( k implementation per model (Epson TM-T20 etc.))

    # -- EventBus handler ------------------------------------------------------

    async def handle_print_request(self, req: PrintRequest) -> None:
        """Subscribe this to EventBus ``PrintRequest`` (main → printer)."""
        if req.raw_bytes:
            await self._write(req.raw_bytes)
            if req.cut_after:
                await self.cut()
            return
        if req.text is None:
            return
        # Route via print_text with style flags
        await self.print_text(
            req.text,
            bold=req.bold,
            double_width=req.double_width,
            double_height=req.double_height,
            fat=req.fat,
            underline=req.underline,
            align=req.align if req.align else None,
            feed_after=req.feed_lines,
        )
        if req.cut_after:
            await self.cut()

    async def print_raw(self, data: bytes) -> None:
        """Write raw ESC/POS bytes (for tests / advanced)."""
        await self._write(data)
