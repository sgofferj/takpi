"""
Flight Illusion GSA-72 Chronometer library.

Python port of ArduIllusion C++ functions for the Chrono
(``ArduIllusion/ArduIllusion.cpp:161`` – ``ArduIllusion/ArduIllusion.cpp:201`` /
``ArduIllusion/ArduIllusion.h:64``).

Hardware: Flight Illusion GSA-072 Chronometer/Clock (Davtron style)
  https://www.flightillusion.com/military-vintage/chrono-gauges-mil/gsa-072-chronometerclock-davtron-style/
  6-byte serial protocol at 38400 baud, TTL 3.3V via Raspberry Pi GPIO UART (GPIO14 TXD / GPIO15 RXD,
  ``/dev/serial0`` → ``/dev/ttyAMA0`` or ``/dev/ttyS0`` depending on Pi model), same framing as other FI gauges:
  ``[0x00, id, cmd|flags, data_low|0x01, data_high|0x02, 0xFF]`` with 2 ms per-byte gap.
  Enable UART via ``raspi-config`` / ``dtoverlay=disable-bt`` and disable serial console;
  see wiring in ``README_chronometer.md``.

Only GSA-72 commands are exposed; general FI commands (RESET/QUERY/LIGHT) are
included for completeness via ``init_gauge``/``query_gauge``.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Optional

import serial  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# ---------------------------------------------------------------------------
# Constants – mirrors ArduIllusion.h:44 / ArduIllusion.h:64
# ---------------------------------------------------------------------------

GSA72_ID: int = 109

CMD_RESET: int = 1
CMD_SETADR: int = 2
CMD_QUERY: int = 7
CMD_SETLIGHT: int = 8

GSA72_CMD_SETLOCAL: int = 4
GSA72_CMD_SETUTC: int = 5
GSA72_CMD_SETFLT: int = 6
GSA72_CMD_SETVOLT: int = 9
GSA72_CMD_SETBUZZER: int = 10
GSA72_CMD_SETTEMPC: int = 11
GSA72_CMD_SETTEMPF: int = 12
GSA72_CMD_SETSEMICOLON: int = 13

# Also used internally for packet flags (see ArduIllusion.cpp:32 sendCommand)
_TERMINATOR: int = 0xFF
_HEADER: int = 0x00


def encode_packet(gauge_id: int, cmd: int, value: int) -> bytes:
    """
    Pure function implementing ``FIGaugeSet::sendCommand`` (ArduIllusion.cpp:32).

    Encodes ``value`` into the 6-byte FI packet. Extracted for testability
    without hardware.

    Mirrors C++::

        buffer[1]=id; buffer[5]=0xff;
        long1 = abs(value); int2 = long1 >> 8;
        buffer[3] = (long1 & 0xff) | 0x01;
        buffer[4] = (int2 & 0xff) | 0x02;
        bytethree = cmd << 4;
        bytethree |= (long1 & 0x01);
        bytethree |= (int2 & 0x02);
        if (value < 0) bytethree |= 0x0C; else bytethree |= 0x08;
        buffer[2]=bytethree;
        buffer[0]=0x00;
    """
    long1: int = abs(int(value))
    int2: int = long1 >> 8

    byte_three: int = (long1 & 0xFF) | 0x01
    byte_four: int = (int2 & 0xFF) | 0x02

    byte_two: int = (cmd << 4) & 0xF0
    byte_two |= long1 & 0x01
    byte_two |= int2 & 0x02
    if value < 0:
        byte_two |= 0x0C
    else:
        byte_two |= 0x08

    return bytes(
        [
            _HEADER,
            gauge_id & 0xFF,
            byte_two & 0xFF,
            byte_three & 0xFF,
            byte_four & 0xFF,
            _TERMINATOR,
        ]
    )


class ChronometerClient:
    """
    Async client for Flight Illusion GSA-72 Chronometer.

    Reimplementation of ``FIGaugeSet`` Chrono methods:
      - ``gsa72_setUTC(long)``             → :meth:`gsa72_set_utc`
      - ``gsa72_setUTC(byte h,m,s)``       → :meth:`gsa72_set_utc_hms`
      - ``gsa72_setFLT(long)``             → :meth:`gsa72_set_flt`
      - ``gsa72_setFLT(byte h,m,s)``       → :meth:`gsa72_set_flt_hms`
      - ``gsa72_setLocal(byte h)``         → :meth:`gsa72_set_local`
      - ``gsa72_setTempC(int decicelsius)``→ :meth:`gsa72_set_temp_c`
      - ``gsa72_setVolt(long mV)``         → :meth:`gsa72_set_volt`
      - ``gsa72_setBuzzer(on/off)``        → :meth:`gsa72_set_buzzer` (cmd 10, discovered 2026-09-10)
      - ``gsa72_setSemicolon``             → :meth:`gsa72_set_semicolon_blink` (cmd 13, low byte 0 = steady)

    Serial writes replicate the Arduino ``delay(2)`` per byte gap.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 38400,
        timeout: float = 1.0,
    ) -> None:
        self.port: str = port
        self.baudrate: int = baudrate
        self.timeout: float = timeout
        self._connection: Optional[serial.Serial] = None
        self._lock: Optional[asyncio.Lock] = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open serial port. Idempotent."""
        if self._connection is not None and self._connection.is_open:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        logger.info(
            "Connecting to GSA-72 Chronometer on %s @%d", self.port, self.baudrate
        )
        loop = asyncio.get_running_loop()
        self._connection = await loop.run_in_executor(
            None,
            lambda: serial.Serial(
                port=self.port, baudrate=self.baudrate, timeout=self.timeout
            ),
        )
        # FI gauges need settle time after open (same as driver.py)
        await asyncio.sleep(2.0)

    async def disconnect(self) -> None:
        """Close serial port."""
        if self._connection is not None and self._connection.is_open:
            loop = asyncio.get_running_loop()
            conn = self._connection
            await loop.run_in_executor(None, conn.close)
        self._connection = None

    async def __aenter__(self) -> ChronometerClient:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # Low-level packet writer – ArduIllusion.cpp:32 sendCommand
    # ------------------------------------------------------------------

    async def _send_command(self, gauge_id: int, cmd: int, value: int) -> None:
        if self._connection is None or not self._connection.is_open:
            raise ConnectionError("Not connected to Chronometer hardware")
        if self._lock is None:
            self._lock = asyncio.Lock()

        packet: bytes = encode_packet(gauge_id, cmd, value)

        loop = asyncio.get_running_loop()
        async with self._lock:
            for b in packet:
                await loop.run_in_executor(None, self._connection.write, bytes([b]))
                await asyncio.sleep(0.002)

    # ------------------------------------------------------------------
    # General FI commands (usable with GSA72_ID)
    # ------------------------------------------------------------------

    async def init_gauge(self, gauge_id: int = GSA72_ID) -> None:
        """``FIGaugeSet::Init`` – RESET (ArduIllusion.cpp:64)."""
        await self._send_command(gauge_id, CMD_RESET, 0)

    async def query_gauge(self, gauge_id: int = GSA72_ID) -> None:
        """``FIGaugeSet::Query`` (ArduIllusion.cpp:68)."""
        await self._send_command(gauge_id, CMD_QUERY, 0)

    async def set_light(self, gauge_id: int, light: int) -> None:
        """``FIGaugeSet::setLight`` (ArduIllusion.cpp:72). Light: DL000000."""
        await self._send_command(gauge_id, CMD_SETLIGHT, light)

    async def set_address(self, gauge_id: int, address: int) -> None:
        """``FIGaugeSet::setAddress`` (ArduIllusion.cpp:76)."""
        await self._send_command(gauge_id, CMD_SETADR, address)

    # ------------------------------------------------------------------
    # GSA-72 Chronometer – direct ports of ArduIllusion.cpp:161-201
    # ------------------------------------------------------------------

    async def gsa72_set_utc(self, seconds: int) -> None:
        """
        ``FIGaugeSet::gsa72_setUTC(long seconds)`` (ArduIllusion.cpp:163).

        ``seconds`` is seconds since midnight 0..65535. C++ wraps >65535 as
        ``65535 - seconds`` – preserved for fidelity.
        """
        if seconds > 65535:
            seconds = 65535 - seconds
        await self._send_command(GSA72_ID, GSA72_CMD_SETUTC, seconds)

    async def gsa72_set_utc_hms(self, hours: int, minutes: int, seconds: int) -> None:
        """
        ``FIGaugeSet::gsa72_setUTC(byte h,m,s)`` (ArduIllusion.cpp:168).

        Helper that converts H:M:S → seconds and delegates to
        :meth:`gsa72_set_utc`. Same 65535 wrap as the long overload.
        """
        value: int = (hours * 3600) + (minutes * 60) + seconds
        if value > 65535:
            value = 65535 - value
        await self._send_command(GSA72_ID, GSA72_CMD_SETUTC, value)

    async def gsa72_set_flt(self, seconds: int) -> None:
        """
        ``FIGaugeSet::gsa72_setFLT(long seconds)`` (ArduIllusion.cpp:174).
        Flight timer in seconds.
        """
        if seconds > 65535:
            seconds = 65535 - seconds
        await self._send_command(GSA72_ID, GSA72_CMD_SETFLT, seconds)

    async def gsa72_set_flt_hms(self, hours: int, minutes: int, seconds: int) -> None:
        """``FIGaugeSet::gsa72_setFLT(byte h,m,s)`` (ArduIllusion.cpp:179)."""
        value: int = (hours * 3600) + (minutes * 60) + seconds
        if value > 65535:
            value = 65535 - value
        await self._send_command(GSA72_ID, GSA72_CMD_SETFLT, value)

    async def gsa72_set_local(self, hours: int) -> None:
        """
        ``FIGaugeSet::gsa72_setLocal(byte hours)`` (ArduIllusion.cpp:185).

        Sets the chronometer's local hour display (0-23 expected).
        """
        await self._send_command(GSA72_ID, GSA72_CMD_SETLOCAL, int(hours))

    async def gsa72_set_temp_c(self, decicelsius: int) -> None:
        """
        ``FIGaugeSet::gsa72_setTempC(int decicelsius)`` (ArduIllusion.cpp:189).

        C++ logic::

            byte tempc = (decicelsius+50)/100;
            byte tempf = 9/5*(tempc+32);      // integer division 9/5==1 → bug
            sendCommand(SETTEMPC, tempc);
            sendCommand(SETTEMPF, tempf);

        Python port preserves ``tempc`` formula exactly and implements the
        *intended* Celsius→Fahrenheit conversion ``1.8*C+32`` instead of the
        C++ integer-division bug, while still sending both commands.
        """
        tempc: int = (decicelsius + 50) // 100
        # Correct conversion; original C++ 9/5 == 1 would give tempc+32
        tempf: int = int((1.8 * tempc) + 32)
        await self._send_command(GSA72_ID, GSA72_CMD_SETTEMPC, tempc)
        await self._send_command(GSA72_ID, GSA72_CMD_SETTEMPF, tempf)

    async def gsa72_set_volt(self, millivolts: int) -> None:
        """
        ``FIGaugeSet::gsa72_setVolt(long millivolts)`` (ArduIllusion.cpp:196).

        C++::

            int volts = millivolts/1000;
            int decivolts = (millivolts - (volts*1000))/100;
            int value = (volts << 8) + decivolts;
        """
        volts: int = millivolts // 1000
        decivolts: int = (millivolts - (volts * 1000)) // 100
        value: int = (volts << 8) + decivolts
        await self._send_command(GSA72_ID, GSA72_CMD_SETVOLT, value)

    async def gsa72_set_buzzer(self, on: bool | int) -> None:
        """
        GSA-72 buzzer – cmd 10 (discovered 2026-09-10, not in ArduIllusion.cpp).

        ``on``: ``True``/``1`` = buzzer on, ``False``/``0`` = off.
        Low byte controls state (``0`` off, ``!=0`` on).
        """
        value: int = (1 if on else 0) if isinstance(on, bool) else int(on)
        await self._send_command(GSA72_ID, GSA72_CMD_SETBUZZER, value)

    async def gsa72_set_semicolon_blink(self, blink: bool | int) -> None:
        """
        GSA-72 semicolon blink on seconds – cmd 13 (discovered 2026-09-10).

        ``blink``: ``True``/``1`` = blinking colon, ``False``/``0`` = steady.
        Per hw docs: low byte ``0`` = no blinking (steady), ``!=0`` = blink.
        """
        value: int = (1 if blink else 0) if isinstance(blink, bool) else int(blink)
        await self._send_command(GSA72_ID, GSA72_CMD_SETSEMICOLON, value)


# Backwards-compatible alias – matches driver.py class name
FlightIllusionChronometerClient = ChronometerClient
