# tak-bridge-escpos

Raspberry Pi bridge for **ESC/POS thermal printers** (TTL serial) via **hardware UART** (`/dev/serial0`, `/dev/ttyUSB0`) or **softserial GPIO bit-bang** (`tx_pin`/`rx_pin` BCM, configurable baudrate). Provides **async** driver with convenience styles **fat** (bold+double), double-height/width, underline, align, feed/cut, plus **911 alarm** helper for the main takpi process (`PrintRequest` on `EventBus` → printer).

- Library: `tak_bridge_escpos.escpos` – `EscPosPrinter` (async, `pyserial` or `pigpio` softserial) + `PrintRequest` + `FakeSerial`
- Service: `tak_bridge_escpos.__main__` – `asyncio` `EventBus` bridge: `PrintRequest` → printer, `CotReceived` (takstream, not pytak) → `print_alarm("911 alarm received from <callsign> at <location>")`

## Detailed Submodule Documentation – `README_<topic>.md`

> Per project rule every submodule gets a thorough `README_<topic>.md`. This bridge has **2 Python submodules** → 2 dedicated docs (plus top-level `../../README.md:15` and `../../README_escpos.md:1` if at top).

| # | Topic | File | Covers |
|---|-------|------|--------|
| 1 | **ESCPOS driver** | [`README_escpos.md`](README_escpos.md) | `src/tak_bridge_escpos/escpos.py:1` – softserial GPIO (`tx_pin`/`rx_pin` BCM, `baudrate` 9600-115200, `pigpio` bit-bang vs `pyserial` hardware), baudrate config, ESC/POS bytes `ESC @`/`GS !`/`ESC E`/`ESC a`, `FakeSerial`, `PrintRequest`, `EscPosPrinter` async `connect`/`_write`/`print_text`/`fat`/`double_height`/`double_width`/`bold`/`print_alarm`/`cut`/`feed`, encoding `cp437`, `is_softserial`, tests |
| 2 | **Main service** | [`README_main.md`](README_main.md) | `src/tak_bridge_escpos/__main__.py:1` – `EventBus` `PrintRequest` → printer, `CotReceived` → 911 auto-print (`_handle_cot_print`), `ESCPOS_PORT`/`ESCPOS_BAUDRATE`/`ESCPOS_TX_PIN`/`ESCPOS_RX_PIN`, `TAK_HOST`/`PORT`/`CERT` for `CotStream` (takstream), `HEALTH_FILE=/tmp/tak-escpos-healthy`, systemd softserial, direct vs bus usage examples, troubleshooting softserial/`pigpiod`/baud mismatch |

Top-level `../../README_escpos.md` (if present) aggregates this bridge; `../../README.md:15` indexes all `README_<topic>.md`.

## Hardware (Configurable Softserial GPIO vs Hardware UART)

- Printer: any TTL ESC/POS thermal (Epson TM-T20, Adafruit, Goojprt, Xprinter, etc.) 58mm/80mm, 5V-12V PSU, TTL RX (printer) ← Pi TX, optional TX→RX
- Interface: **Choice**:
  - **Hardware serial** – `ESCPOS_PORT=/dev/serial0` (GPIO14/15 hardware UART, shared with chronometer? Use softserial if conflict) or `/dev/ttyUSB0` (USB-TTL dongle) at **configurable baudrate** 9600 (DIP default) / 19200 / 38400 / 57600 / 115200
  - **Softserial GPIO** – `ESCPOS_TX_PIN=27` (BCM, header pin 13 → printer RX) + optional `ESCPOS_RX_PIN=22` (pin 15 ← printer TX, rarely needed) via `pigpio` daemon bit-bang (4.7k? direct 3.3V TTL; printer 5V tolerant often, level shifter if 5V→Pi)
- Baudrate: **Configurable** via `ESCPOS_BAUDRATE` (must match printer DIP switches / `GS` config). Common 9600 (default) or 19200 (faster). Set via `EscPosPrinter(baudrate=...)` or `.env`.
- Pins: **Configurable** `tx_pin`/`rx_pin` BCM numbers (not WiringPi). Example softserial: `tx_pin=27` (BCM27 pin13) → printer RX (yellow), `rx_pin=22` (pin15) ← printer TX (green) if status/Busy needed, else `rx_pin=None` TX-only.
- No USB `by-id` required for softserial; hardware uses stable `/dev/serial/by-id/` or `/dev/serial0`. See `README_escpos.md:2.1` for wiring diagrams + `raspi-config` + `pigpiod` enable.
- Power: Printer external 5-12V 2A PSU (not Pi 5V); share GND (Pi pin6/14 ↔ printer GND).

Protocol: ESC/POS `ESC @` init, `ESC E` bold, `GS ! n` size (`0x00` normal, `0x01` double height, `0x10` double width, `0x11` fat), `ESC a` align, `ESC d` feed, `GS V` cut, `cp437` encoding, `0.005 s` gap per write (`escpos.py:1`).

## Wiring (Pi → Printer, Condensed)

```
Hardware UART (if free):
  Pi pin8 GPIO14 TXD (/dev/serial0) ──→ Printer RX (TTL)
  Pi pin10 GPIO15 RXD ──← Printer TX (optional, rarely used)
  Pi GND (6/14) ─────────── Printer GND

Softserial GPIO (recommended when /dev/serial0 used by chronometer):
  Pi BCM27 pin13 (GPIO27) ──→ Printer RX  @9600-115200 bit-bang via pigpiod
  Pi BCM22 pin15 (GPIO22) ──← Printer TX (optional, status)
  Pi GND ─────────────────── Printer GND
  Enable: sudo pigpiod; set tx_pin=27 in .env; baudrate must match printer DIP
```

See `README_escpos.md:2` for full diagrams, `pigpiod`/systemd, level shifter, and 58mm paper feed.

## Library – EscPosPrinter (Async, Softserial + Hardware)

```python
import asyncio
from tak_bridge_escpos.escpos import EscPosPrinter

# Hardware UART
async def hw():
    async with EscPosPrinter(port="/dev/serial0", baudrate=9600) as p:
        await p.fat("HELLO", align="center")
        await p.print_alarm(callsign="ALPHA", lat=60.1, lon=24.8, location="Helsinki")

# Softserial GPIO (when hardware UART busy)
async def soft():
    async with EscPosPrinter(tx_pin=27, rx_pin=22, baudrate=19200) as p:  # BCM pins
        await p.fat("911 ALARM")
        await p.double_height("double height")
        await p.print_text("left", align="left", bold=True)
        await p.cut()

asyncio.run(hw())
```

Convenience styles `escpos.py:1`:
- `fat(text)` → bold + double width+height (`GS ! 0x11`, `ESC E`), centered by default
- `double_height(text)`, `double_width(text)`, `bold(text)`, `set_size(w,h)`, `set_align(left/center/right)`, `set_underline`, `feed`, `cut`, `print_alarm`, `print_qr`

## Service – Main Process Can Print (EventBus + Cot)

`src/tak_bridge_escpos/__main__.py:1` bridges `PrintRequest` and `CotReceived` → printer:

```python
from takpi_common.bus import EventBus
from tak_bridge_escpos.escpos import PrintRequest

bus = EventBus()
printer = EscPosPrinter(tx_pin=27, baudrate=9600)
await printer.connect()
bus.subscribe(PrintRequest, printer.handle_print_request)
# Main process can now print from anywhere:
await bus.publish(PrintRequest(text="Hello", fat=True, cut_after=True))
# Or 911 alarm auto-print via Cot:
# CotReceived(b-a-o-tif) → _handle_cot_print → printer.print_alarm(callsign, lat, lon, location)
await bus.publish(PrintRequest(text="911 alarm received from ALPHA at 60.1,24.8 Helsinki", fat=True))
```

**911 example** `escpos.py:1` `print_alarm` prints:
```
*** 911 ALARM ***
From: ALPHA
Loc:  60.12345, 24.98765
      Helsinki
Time: 2026-09-07 12:34:56 UTC
Type: b-a-o-tif
Remarks: car crash
--------------------------------
```

See `README_main.md:1` for full EventBus + `CotBus` (takstream) wiring, `ESCPOS_*` env, `TAK_HOST` streaming, and direct vs bus examples.

### Env / .env (Softserial GPIO vs Hardware)

| Var | Default | Notes |
|---|---|---|
| `ESCPOS_PORT` / `PRINTER_PORT` | `/dev/serial0` or `None` if `TX_PIN` set | Hardware port – `/dev/ttyUSB0` USB-TTL or `/dev/serial0` GPIO UART. Mutually exclusive with `TX_PIN`. |
| `ESCPOS_BAUDRATE` / `PRINTER_BAUDRATE` | `9600` | Must match printer DIP (9600/19200/38400/57600/115200) |
| `ESCPOS_TX_PIN` / `PRINTER_TX_PIN` | `None` | BCM TX pin for softserial (e.g. `27` → pin13) – enables `pigpio` bit-bang, `ESCPOS_PORT` ignored if set |
| `ESCPOS_RX_PIN` / `PRINTER_RX_PIN` | `None` | BCM RX pin for softserial (optional, e.g. `22`) |
| `ESCPOS_ENCODING` | `cp437` | `cp437` or `utf8` |
| `ESCPOS_FAKE` | `0` | `1` → `FakeSerial` (CI, no hardware/pigpio) |
| `TAK_HOST` / `API_HOST` | `None` | If set, service auto-connects `CotStream` (takstream) and prints 911 on `CotReceived` |
| `TAK_PORT` | `8089` | TAK streaming port (8087 plaintext, 8089 TLS) |
| `HEALTH_FILE` | `/tmp/tak-escpos-healthy` | `dialout`+`gpio` groups needed |
| `DEBUG` | `0` | `1` → DEBUG |

`.env` examples:

```
# Softserial GPIO (recommended, hardware UART free for chronometer)
ESCPOS_TX_PIN=27
ESCPOS_RX_PIN=22
ESCPOS_BAUDRATE=9600

# Hardware UART (if /dev/serial0 free)
ESCPOS_PORT=/dev/serial0
ESCPOS_BAUDRATE=19200

# With TAK 911 auto-print
TAK_HOST=tak.example.com
TAK_PORT=8089
TAK_CERT=certs/client.pem
TAK_KEY=certs/client.key
TAK_CALLSIGN=PI-PRINTER
```

See `README_escpos.md:4` and `README_main.md:4` for full tables + `pigpiod` + `dialout`/`gpio` permissions.

### Run (Softserial GPIO or Hardware)

```bash
# Enable softserial daemon (for GPIO TX_PIN)
sudo pigpiod
# Enable hardware UART if using ESCPOS_PORT=/dev/serial0 (see tak-bridge-chronometer docs)

poetry install --directory tak-bridge-escpos
# Softserial (BCM27)
ESCPOS_TX_PIN=27 ESCPOS_BAUDRATE=9600 poetry run --directory tak-bridge-escpos start
# Hardware
ESCPOS_PORT=/dev/ttyUSB0 ESCPOS_BAUDRATE=19200 poetry run --directory tak-bridge-escpos start
# Fake (CI, no hardware)
ESCPOS_FAKE=1 poetry run --directory tak-bridge-escpos start
# Logs
journalctl -u tak-bridge-escpos -f
cat /tmp/tak-escpos-healthy
```

Without hardware, tests use `FakeSerial`/`FakePigpio` – no Pi/pigpio needed: `poetry run --directory tak-bridge-escpos pytest -v`.

## Systemd (Softserial GPIO)

See `systemd/tak-bridge-escpos.service`. Install on Pi:

```bash
sudo systemctl enable pigpiod  # for softserial
sudo cp systemd/tak-bridge-escpos.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tak-bridge-escpos
# pigpiod must be running if ESCPOS_TX_PIN set
```

Requires `dialout` (hardware) + `gpio` (pigpio) groups: `sudo usermod -a -G dialout,gpio $USER`.

## Testing

```bash
poetry run --directory tak-bridge-escpos pytest -v  # 9 tests, FakeSerial + EventBus + Cot→print, no hardware
poetry run --directory tak-bridge-escpos black . && poetry run --directory tak-bridge-escpos mypy src && poetry run --directory tak-bridge-escpos pylint src
```

## References

- Source: `escpos.py:1` (`EscPosPrinter`, `SoftSerial`, `HardwareSerial`, `FakeSerial`, `PrintRequest`) + `__main__.py:1` (EventBus + Cot 911)
- Manual: ESC/POS `ESC @`/`GS !`/`ESC E`/`ESC a`, 58mm paper, `pigpio` `wave_add_serial`/`bb_serial_read_open`
- Thorough docs: [`README_escpos.md`](README_escpos.md) (driver, softserial GPIO, baudrate, styles, wiring, `pigpiod`), [`README_main.md`](README_main.md) (EventBus + Cot 911, usage examples, troubleshooting `pigpiod`/baud), [`../../README_escpos.md`](../../README_escpos.md) (monorepo aggregation)
- Common: `takpi_common/bus.py:1` (`EventBus`), `takpi_common/cot_bus.py:1` (`CotBus` takstream), `takpi/python-tak-cot-streaming` (takstream, not pytak)
- Tests: `tests/test_escpos.py:1`
