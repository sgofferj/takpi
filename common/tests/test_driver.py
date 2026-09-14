"""Tests for MCP23017 driver + FakeSMBus."""

import pytest

from takpi_common.mcp23017.driver import FakeSMBus, MCP23017


def test_fake_smbus_defaults() -> None:
    fake = FakeSMBus()
    dev = MCP23017(address=0x20, smbus=fake)
    # Defaults: IODIR all inputs
    assert dev.read_reg(0x00) == 0xFF
    assert dev.read_reg(0x01) == 0xFF


def test_direction_and_pullup() -> None:
    fake = FakeSMBus()
    dev = MCP23017(address=0x20, smbus=fake)
    dev.setup(iodir_a=0xFF, iodir_b=0x00)
    assert dev.get_direction(0) is True  # GPA0 input
    assert dev.get_direction(8) is False  # GPB0 output
    dev.set_direction(0, False)
    assert dev.get_direction(0) is False
    dev.set_pullup(1, True)
    assert fake._regs[0x20][0x0C] & (1 << 1)  # GPPUA bit1
    dev.set_pullup(1, False)
    assert not (fake._regs[0x20][0x0C] & (1 << 1))


def test_gpio_read_write() -> None:
    fake = FakeSMBus()
    dev = MCP23017(address=0x20, smbus=fake)
    dev.setup(iodir_a=0xFF, iodir_b=0x00)
    # Input pin 0 driven low (pressed)
    fake.set_input(0x20, 0, False)
    assert dev.read_pin(0) is False
    fake.set_input(0x20, 0, True)
    assert dev.read_pin(0) is True
    # Output pin 8
    dev.write_pin(8, True)
    assert fake.get_olat(0x20, 8) is True
    assert dev.read_pin(8) is True  # output reflects latch
    dev.write_pin(8, False)
    assert fake.get_olat(0x20, 8) is False


def test_word_read_write() -> None:
    fake = FakeSMBus()
    dev = MCP23017(address=0x20, smbus=fake)
    dev.setup(iodir_a=0x00, iodir_b=0x00)  # all outputs
    dev.write_gpio(0xAA55)
    assert dev.read_gpio() == 0xAA55
    # Mixed: set inputs
    dev.setup(iodir_a=0xFF, iodir_b=0xFF)
    fake.set_input(0x20, 0, True)
    fake.set_input(0x20, 8, True)
    gpio = dev.read_gpio()
    assert (gpio >> 0) & 1 == 1
    assert (gpio >> 8) & 1 == 1


def test_address_validation() -> None:
    with pytest.raises(ValueError):
        MCP23017(address=0x19, smbus=FakeSMBus())
    with pytest.raises(ValueError):
        MCP23017(address=0x28, smbus=FakeSMBus())
    # Valid
    MCP23017(address=0x27, smbus=FakeSMBus())


def test_daisy_chain_two_devices_isolated() -> None:
    fake = FakeSMBus()
    dev0 = MCP23017(address=0x20, smbus=fake)
    dev1 = MCP23017(address=0x21, smbus=fake)
    dev0.setup()
    dev1.setup()
    dev0.write_pin(0, True)
    dev1.write_pin(0, False)
    assert fake.get_olat(0x20, 0) is True
    assert fake.get_olat(0x21, 0) is False
    # Input isolation
    fake.set_input(0x20, 1, True)
    fake.set_input(0x21, 1, False)
    assert dev0.read_pin(1) is True
    assert dev1.read_pin(1) is False
