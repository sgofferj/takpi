"""tak-bridge-escpos – async ESC/POS thermal printer driver (softserial GPIO)."""

from tak_bridge_escpos.escpos import EscPosPrinter, FakeSerial, PrintRequest

__all__ = ["EscPosPrinter", "FakeSerial", "PrintRequest"]
__version__ = "0.1.0"
