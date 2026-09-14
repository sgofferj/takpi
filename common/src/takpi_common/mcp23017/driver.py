"""
MCP23017 I2C I/O expander driver for takpi.

Supports daisy-chained devices (addresses 0x20-0x27 via A0-A2) on a single
I2C bus (default bus 1 on Pi). Register map for BANK=0 (sequential).

Protocol: each 16-bit port is split into A (0-7) and B (8-15). Driver abstracts
pin number 0-15 → port/bit.

Designed for testability: inject ``FakeSMBus`` or any object with
``read_byte_data``/``write_byte_data``/``read_word_data`` etc.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Registers BANK=0
IODIRA = 0x00
IODIRB = 0x01
IPOLA = 0x02
IPOLB = 0x03
GPINTENA = 0x04
GPINTENB = 0x05
DEFVALA = 0x06
DEFVALB = 0x07
INTCONA = 0x08
INTCONB = 0x09
IOCON = 0x0A
GPPUA = 0x0C
GPPUB = 0x0D
INTFA = 0x0E
INTFB = 0x0F
INTCAPA = 0x10
INTCAPB = 0x11
GPIOA = 0x12
GPIOB = 0x13
OLATA = 0x14
OLATB = 0x15

# IOCON bits
IOCON_BANK = 0x80
IOCON_MIRROR = 0x40
IOCON_SEQOP = 0x20
IOCON_DISSLW = 0x10
IOCON_HAEN = 0x08
IOCON_ODR = 0x04
IOCON_INTPOL = 0x02


class FakeSMBus:
    """
    In-memory fake for tests – no hardware.

    Stores 256 registers per address, implements subset of smbus2.SMBus API.
    """

    def __init__(self, bus_id: int = 1) -> None:
        self.bus_id: int = bus_id
        self._regs: dict[int, bytearray] = {}

    def _ensure(self, addr: int) -> bytearray:
        if addr not in self._regs:
            arr = bytearray(256)
            # Default power-on defaults per datasheet: IODIR=0xFF (inputs)
            arr[IODIRA] = 0xFF
            arr[IODIRB] = 0xFF
            self._regs[addr] = arr
        return self._regs[addr]

    def write_byte_data(self, addr: int, reg: int, value: int) -> None:
        regs = self._ensure(addr)
        regs[reg & 0xFF] = value & 0xFF
        # Mirror GPIO writes to OLAT for reads
        if reg == GPIOA:
            regs[OLATA] = value & 0xFF
        elif reg == GPIOB:
            regs[OLATB] = value & 0xFF

    def read_byte_data(self, addr: int, reg: int) -> int:
        regs = self._ensure(addr)
        # GPIO reads: if pin is output, return OLAT; if input, return GPIO reg
        if reg in (GPIOA, GPIOB):
            iodir = regs[IODIRA] if reg == GPIOA else regs[IODIRB]
            olat = regs[OLATA] if reg == GPIOA else regs[OLATB]
            gpio = regs[reg]
            # For outputs (iodir 0), GPIO reflects OLAT
            # For inputs (iodir 1), GPIO is as set externally via set_input()
            # We simulate by merging: output bits from olat, input bits from gpio
            result = 0
            for bit in range(8):
                if (iodir >> bit) & 1:
                    # input – keep gpio
                    if (gpio >> bit) & 1:
                        result |= 1 << bit
                else:
                    if (olat >> bit) & 1:
                        result |= 1 << bit
            return result
        return regs[reg & 0xFF]

    def write_word_data(self, addr: int, reg: int, value: int) -> None:
        # Sequential write A then B (if reg even)
        self.write_byte_data(addr, reg, value & 0xFF)
        self.write_byte_data(addr, reg + 1, (value >> 8) & 0xFF)

    def read_word_data(self, addr: int, reg: int) -> int:
        lo = self.read_byte_data(addr, reg)
        hi = self.read_byte_data(addr, reg + 1)
        return (hi << 8) | lo

    # Helpers for tests
    def set_input(self, addr: int, pin: int, value: bool) -> None:
        """Force input pin state (as if external button drives it)."""
        regs = self._ensure(addr)
        gpio_reg = GPIOA if pin < 8 else GPIOB
        bit = pin % 8
        if value:
            regs[gpio_reg] |= 1 << bit
        else:
            regs[gpio_reg] &= ~(1 << bit) & 0xFF

    def get_olat(self, addr: int, pin: int) -> bool:
        regs = self._ensure(addr)
        reg = OLATA if pin < 8 else OLATB
        return bool((regs[reg] >> (pin % 8)) & 1)


class MCP23017:
    """
    Driver for a single MCP23017 at ``address`` on I2C ``bus``.

    For daisy-chain, create multiple instances at 0x20-0x27 sharing the same
    bus object. All operations are synchronous; the manager wraps them in
    ``run_in_executor`` for asyncio.

    Example::

        bus = SMBus(1)  # real hardware
        dev0 = MCP23017(bus=bus, address=0x20)
        await dev0.setup()  # or dev0.init_sync()
        dev0.set_direction(0, True)  # pin 0 input
        dev0.set_pullup(0, True)
        print(dev0.read_pin(0))
    """

    def __init__(
        self,
        address: int = 0x20,
        bus: int = 1,
        smbus: Any | None = None,
    ) -> None:
        if not 0x20 <= address <= 0x27:
            raise ValueError(f"MCP23017 address must be 0x20-0x27, got {hex(address)}")
        self.address: int = address
        self.bus_id: int = bus
        self._smbus: Any = smbus
        self._owned_bus: bool = False

    def _bus(self) -> Any:
        if self._smbus is not None:
            return self._smbus
        # Lazy import so tests without smbus2 still collect
        try:
            from smbus2 import SMBus  # type: ignore[import-not-found]

            bus = SMBus(self.bus_id)
            self._smbus = bus
            self._owned_bus = True
            return bus
        except ImportError as exc:
            raise RuntimeError(
                "smbus2 not installed – install takpi-common or inject FakeSMBus"
            ) from exc

    def close(self) -> None:
        if self._owned_bus and self._smbus is not None:
            try:
                self._smbus.close()
            except Exception:
                pass
        self._smbus = None

    # -- low-level reg helpers -------------------------------------------------

    def write_reg(self, reg: int, value: int) -> None:
        self._bus().write_byte_data(self.address, reg, value & 0xFF)

    def read_reg(self, reg: int) -> int:
        return int(self._bus().read_byte_data(self.address, reg) & 0xFF)

    def _pin_to_reg_bit(self, pin: int) -> tuple[int, int]:
        if not 0 <= pin <= 15:
            raise ValueError(f"pin must be 0-15, got {pin}")
        return (pin % 8, pin // 8)  # bit, port (0=A,1=B)

    def _reg_for_pin(self, base_a: int, base_b: int, pin: int) -> tuple[int, int]:
        bit, port = self._pin_to_reg_bit(pin)
        reg = base_a if port == 0 else base_b
        return reg, bit

    # -- configuration ---------------------------------------------------------

    def setup(
        self,
        iodir_a: int = 0xFF,
        iodir_b: int = 0xFF,
        gppu_a: int = 0x00,
        gppu_b: int = 0x00,
    ) -> None:
        """Initialize IOCON (sequential, no mirror) and directions/pullups."""
        # IOCON: SEQOP=0 (sequential), BANK=0, MIRROR=0
        self.write_reg(IOCON, 0x00)
        self.write_reg(IODIRA, iodir_a & 0xFF)
        self.write_reg(IODIRB, iodir_b & 0xFF)
        self.write_reg(GPPUA, gppu_a & 0xFF)
        self.write_reg(GPPUB, gppu_b & 0xFF)
        # Clear interrupts
        self.write_reg(GPINTENA, 0x00)
        self.write_reg(GPINTENB, 0x00)

    def set_direction(self, pin: int, is_input: bool) -> None:
        """Set pin direction: True=input, False=output."""
        reg, bit = self._reg_for_pin(IODIRA, IODIRB, pin)
        val = self.read_reg(reg)
        if is_input:
            val |= 1 << bit
        else:
            val &= ~(1 << bit) & 0xFF
        self.write_reg(reg, val)

    def get_direction(self, pin: int) -> bool:
        reg, bit = self._reg_for_pin(IODIRA, IODIRB, pin)
        return bool((self.read_reg(reg) >> bit) & 1)

    def set_pullup(self, pin: int, enable: bool) -> None:
        """Enable 100k pull-up on input pin (only when input)."""
        reg, bit = self._reg_for_pin(GPPUA, GPPUB, pin)
        val = self.read_reg(reg)
        if enable:
            val |= 1 << bit
        else:
            val &= ~(1 << bit) & 0xFF
        self.write_reg(reg, val)

    def set_polarity(self, pin: int, inverted: bool) -> None:
        reg, bit = self._reg_for_pin(IPOLA, IPOLB, pin)
        val = self.read_reg(reg)
        if inverted:
            val |= 1 << bit
        else:
            val &= ~(1 << bit) & 0xFF
        self.write_reg(reg, val)

    # -- GPIO read/write -------------------------------------------------------

    def read_gpio(self) -> int:
        """Read 16-bit GPIO (B<<8 | A). Input pins reflect pin state, outputs reflect latch."""
        # Sequential read from GPIOA (reads A then B)
        try:
            # Use word read for efficiency
            return int(self._bus().read_word_data(self.address, GPIOA) & 0xFFFF)
        except AttributeError:
            lo = self.read_reg(GPIOA)
            hi = self.read_reg(GPIOB)
            return (hi << 8) | lo

    def write_gpio(self, value: int) -> None:
        """Write 16-bit OLAT (B<<8 | A) – only output pins driven."""
        lo = value & 0xFF
        hi = (value >> 8) & 0xFF
        self.write_reg(OLATA, lo)
        self.write_reg(OLATB, hi)

    def read_pin(self, pin: int) -> bool:
        """Read single pin (0-15)."""
        gpio = self.read_gpio()
        return bool((gpio >> pin) & 1)

    def write_pin(self, pin: int, value: bool) -> None:
        """Write single output pin (does not affect direction)."""
        reg, bit = self._reg_for_pin(OLATA, OLATB, pin)
        # Need RMW on OLAT, not GPIO
        cur = self.read_reg(reg)
        if value:
            cur |= 1 << bit
        else:
            cur &= ~(1 << bit) & 0xFF
        self.write_reg(reg, cur)

    # -- interrupts (optional, for GPIO INT pin) -------------------------------

    def enable_interrupt(
        self, pin: int, compare_to_defval: bool = False, defval: bool = False
    ) -> None:
        """Enable interrupt-on-change for pin. If compare_to_defval True, compare to defval."""
        reg_en, bit = self._reg_for_pin(GPINTENA, GPINTENB, pin)
        val = self.read_reg(reg_en)
        val |= 1 << bit
        self.write_reg(reg_en, val)

        reg_con, _ = self._reg_for_pin(INTCONA, INTCONB, pin)
        con = self.read_reg(reg_con)
        if compare_to_defval:
            con |= 1 << bit
        else:
            con &= ~(1 << bit) & 0xFF
        self.write_reg(reg_con, con)

        if compare_to_defval:
            reg_def, _ = self._reg_for_pin(DEFVALA, DEFVALB, pin)
            defv = self.read_reg(reg_def)
            if defval:
                defv |= 1 << bit
            else:
                defv &= ~(1 << bit) & 0xFF
            self.write_reg(reg_def, defv)

    def disable_interrupt(self, pin: int) -> None:
        reg_en, bit = self._reg_for_pin(GPINTENA, GPINTENB, pin)
        val = self.read_reg(reg_en)
        val &= ~(1 << bit) & 0xFF
        self.write_reg(reg_en, val)

    def read_int_flags(self) -> int:
        """Read INTF (which pins caused interrupt)."""
        try:
            return int(self._bus().read_word_data(self.address, INTFA) & 0xFFFF)
        except AttributeError:
            return (self.read_reg(INTFB) << 8) | self.read_reg(INTFA)

    def read_int_captured(self) -> int:
        """Read INTCAP (captured GPIO at interrupt)."""
        try:
            return int(self._bus().read_word_data(self.address, INTCAPA) & 0xFFFF)
        except AttributeError:
            return (self.read_reg(INTCAPB) << 8) | self.read_reg(INTCAPA)

    def configure_interrupt_mirror(
        self, mirror: bool = False, open_drain: bool = False, active_high: bool = False
    ) -> None:
        """Configure IOCON INT mirroring / polarity."""
        val = 0
        if mirror:
            val |= IOCON_MIRROR
        if open_drain:
            val |= IOCON_ODR
        if active_high:
            val |= IOCON_INTPOL
        self.write_reg(IOCON, val & 0xFF)
