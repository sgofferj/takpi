# README_escpos – tak-bridge-escpos Bridge (Async ESC/POS, Softserial GPIO)

> **Monorepo:** `takpi/`  
> **Bridge path:** `tak-bridge-escpos/`  
> **Status:** ✅ Implemented – async driver + 911 alarm printing via EventBus  
> **Hardware:** ESC/POS TTL thermal printer (Epson TM-T20, Adafruit Mini, Xprinter, Goojprt, etc.) – `serial:softserial BCM TX/RX @9600-115200` or `serial:/dev/serial0|/dev/ttyUSB0`  
> **Docs:** Bridge `tak-bridge-escpos/README.md:1`, Driver `tak-bridge-escpos/README_escpos.md:1`, Service `tak-bridge-escpos/README_main.md:1`, Common `common/README_bus.md:1` (`EventBus`), `common/README_cot.md:1` (takstream)

This is the **top-level thorough documentation** for the `tak-bridge-escpos` submodule of `takpi`. It aggregates driver and service docs into one entry for `README.md:15` (now 11 `README_<topic>.md` total).

---

## 1. At a Glance

| Attribute | Value |
|-----------|-------|
| Bridge name | `tak-bridge-escpos` (PyPI `tak-bridge-escpos`) |
| Python package | `tak_bridge_escpos` (`src/tak_bridge_escpos/`) |
| Entry | `poetry run start` → `tak_bridge_escpos.__main__:run` |
| Library | `src/tak_bridge_escpos/escpos.py:1` – `EscPosPrinter` (async, `pyserial` hardware vs `pigpio` softserial BCM) |
| Service | `src/tak_bridge_escpos/__main__.py:1` – `EventBus` `PrintRequest` → printer + `CotReceived` (takstream) → `print_alarm` |
| Interface | TTL serial 5V/3.3V, **configurable baudrate** 9600-115200 (DIP), **configurable softserial GPIO** `tx_pin`/`rx_pin` (BCM) via `pigpio` bit-bang **or** hardware `port` `/dev/serial0`/`/dev/ttyUSB0` via `pyserial`; 58mm paper, `ESC @` init, `GS !` size, `ESC E` bold |
| Event | `PrintRequest` (`escpos.py:42`) on `EventBus` (`takpi_common/bus.py:1`) – main → printer; `CotReceived` → main → printer (911) |
| Submodule | `python-tak-cot-streaming` (`../python-tak-cot-streaming`, takstream `CotStream`/`CotEvent`, not pytak) via `CotBus` |
| Systemd | `tak-bridge-escpos/systemd/tak-bridge-escpos.service:1` (needs `pigpiod` if softserial) |
| Health | `/tmp/tak-escpos-healthy` (absent = healthy) |

**What it does:** Main takpi process can print paper reports **e.g. `911 alarm received from <callsign> at <location>`** on a Pi-attached thermal printer, triggered by buttons/encoders → `PrintRequest`, or automatically on CoT emergency (`b-a-o-tif`) via `CotBus` (takstream). Convenience styles **fat** (bold+double), double-height/width, underline, align center/right, feed/cut, QR placeholder, `cp437`.

---

## 2. Why This Bridge

Thermal ESC/POS printers are ideal for takpi field stations: silent, no ink, 58mm paper cut, TTL 5V. On Pi, hardware UART is already used by `tak-bridge-chronometer` (`/dev/serial0` GPIO14/15). **Softserial GPIO** solves conflict – printer on spare BCM pins (e.g. `BCM27` pin13 TX) via `pigpio` bit-bang, **configurable** `tx_pin`/`rx_pin`/`baudrate` per printer DIP. Main process prints **without blocking** (async `run_in_executor` + `asyncio.Lock`), and uses `EventBus` so any takpi component (button, CoT, timer) can request prints.

---

## 3. Hardware & Wiring

### 3.1 Printer

- Types: Epson TM-T20-II, Adafruit Mini Thermal 58mm, Xprinter XP-N160I, Goojprt PT-210 – all ESC/POS TTL (RX only for most, TX optional for status)
- Interface: **TTL serial** 3.3V/5V (check printer manual – many 5V tolerant, Pi GPIO 3.3V logic is usually enough for RX; use level shifter if printer requires 5V→Pi)
- Baudrate: **Configurable** DIP switches or `GS` command – common **9600** (default), also 19200/38400/57600/115200. Must match `ESCPOS_BAUDRATE` env and `EscPosPrinter(baudrate=...)`.
- Paper: 58mm thermal roll, 32 chars/line (Font A, normal), 42 chars Font B; cut via `GS V`.

### 3.2 Wiring – Softserial GPIO (Recommended, No UART Conflict)

```
Pi 40-pin (BCM)                ESC/POS Printer (TTL, JST/PH)
----------------              ------------------------------
Config A – TX-only (most printers, status not needed):
  BCM27 pin13 (GPIO27) ──────► Printer RX (TTL, yellow)
  GND pin6/14 ─────────────── Printer GND (black)
  Printer VCC 5-9V ──── External PSU 5V2A (not Pi 5V) + common GND

Config B – TX/RX (status/busy, optional):
  BCM27 pin13 ──► Printer RX
  BCM22 pin15 (GPIO22) ◄── Printer TX (green, optional, 3.3V via divider if 5V)
  GND common

Hardware UART alternative (if /dev/serial0 free, no softserial):
  Pi pin8 GPIO14 TXD (/dev/serial0) ──► Printer RX
  Pi pin10 GPIO15 RXD ──← Printer TX (optional)
  Pi GND ── Printer GND
  Port: /dev/serial0 @baudrate (or /dev/ttyUSB0 via USB-TTL dongle, /dev/serial/by-id)
```

**Pull-ups:** Printer RX has internal pull-up; no external needed. For softserial, `pigpio` sets `OUTPUT`/`INPUT`.

**Enable softserial daemon:**

```bash
sudo pigpiod  # daemon for bit-bang, must run before printer connect
sudo systemctl enable pigpiod
# Check BCM pins free: no other I2C/SPI on 27/22, no chronometer on same pins
```

**Enable hardware UART (if using port, not softserial):** see `tak-bridge-chronometer/README_chronometer.md:2.1` – `raspi-config` → Serial → No login shell / Yes hardware, `enable_uart=1`, remove `console=serial0`, `systemctl disable serial-getty@serial0`.

**Permissions:** Hardware needs `dialout` (`/dev/serial0`, `/dev/ttyUSB0`); softserial via `pigpio` needs `gpio` group (`sudo usermod -a -G dialout,gpio $USER`).

### 3.3 Baudrate DIP

Most printers have DIP switches 1-3 for baud. Example Epson TM-T20:
- 9600: DIP 7 OFF 8 OFF
- 19200: DIP 7 ON 8 OFF
Check manual; mismatch → garbage. Test via `poetry run --directory tak-bridge-escpos python -c "import asyncio; from tak_bridge_escpos.escpos import EscPosPrinter; ..."` with different baudrates.

---

## 4. Protocol Summary (ESC/POS)

Full driver `tak-bridge-escpos/README_escpos.md:3`.

| Cmd | Bytes | Python | Meaning |
|-----|-------|--------|---------|
| `ESC @` | `1b 40` | `init_printer()` | Initialize |
| `ESC E n` | `1b 45 n` | `set_bold(True)` | Bold `n=1` / off `0` |
| `GS ! n` | `1d 21 n` | `set_size(w,h)` / `set_fat()` | Size: `n=(w-1)<<4|(h-1)`, `0x00` normal, `0x11` fat (2× width+height) |
| `ESC - n` | `1b 2d n` | `set_underline()` | `n=1` on |
| `ESC a n` | `1b 61 n` | `set_align("center")` | `0` left, `1` center, `2` right |
| `ESC d n` | `1b 64 n` | `feed(3)` | Feed `n` lines |
| `GS V m` | `1d 56 m` | `cut(partial=False)` | `m=0` full, `1` partial |
| Text | `cp437` bytes | `print_text("hi")` | `cp437` encode, auto `\n` |

**Fat** = `ESC E 1` + `GS ! 0x11` + centered (via `fat()` `escpos.py:265`).

---

## 5. Submodules (Driver vs Service)

| Submodule | File | Doc | Purpose |
|-----------|------|-----|---------|
| `escpos` driver | `tak-bridge-escpos/src/tak_bridge_escpos/escpos.py:1` | `tak-bridge-escpos/README_escpos.md:1` | `EscPosPrinter` async, `FakeSerial`/`FakePigpio`, `SoftSerial` (pigpio `wave_add_serial`), `HardwareSerial` (pyserial), `PrintRequest`, `fat`/`double_height`/`print_alarm`/`cut`, softserial `tx_pin`/`rx_pin`/`baudrate` |
| `__main__` service | `tak-bridge-escpos/src/tak_bridge_escpos/__main__.py:1` | `tak-bridge-escpos/README_main.md:1` | `EventBus` `PrintRequest`→printer, `CotReceived`→911 alarm `print_alarm(callsign, lat/lon, location)`, `ESCPOS_*` env (port vs tx_pin), `TAK_HOST` for `CotBus` (takstream), health `/tmp/tak-escpos-healthy` |

Also `tak-bridge-escpos/tests/test_escpos.py:1` with `FakeSerial` (no Pi/pigpio).

---

## 6. Quick Start

### 6.1 Install

```bash
cd ~/Dev/TAK/takpi
poetry install --directory tak-bridge-escpos  # pulls takpi-common + takstream path develop, pyserial, pigpio optional
# For softserial GPIO, also:
sudo pigpiod
sudo usermod -a -G dialout,gpio $USER; newgrp dialout
```

### 6.2 Configure `.env` – Softserial GPIO (Recommended)

```
ESCPOS_TX_PIN=27
ESCPOS_RX_PIN=22
ESCPOS_BAUDRATE=9600
# Optional TAK 911 auto-print
TAK_HOST=tak.example.com
TAK_PORT=8089
TAK_CERT=certs/client.pem
TAK_KEY=certs/client.key
TAK_CALLSIGN=PI-PRINTER
```

Hardware UART alternative:

```
ESCPOS_PORT=/dev/serial0
ESCPOS_BAUDRATE=19200
```

Bench CI (no hardware/pigpio):

```
ESCPOS_FAKE=1
```

### 6.3 Run Foreground

```bash
poetry run --directory tak-bridge-escpos start
# logs: "EscPos softserial opened BCM TX=27 RX=22 @9600" or "hardware serial opened /dev/serial0"
# Trigger print via EventBus or CoT 911
```

Check printer: should init (`ESC @`) and be ready. Test fat:

```bash
poetry run --directory tak-bridge-escpos python -c "
import asyncio
from tak_bridge_escpos.escpos import EscPosPrinter
async def t():
    async with EscPosPrinter(tx_pin=27, baudrate=9600) as p:
        await p.fat('HELLO')
        await p.print_alarm(callsign='ALPHA', lat=60.1, lon=24.8, location='Helsinki')
asyncio.run(t())
"
```

### 6.4 Tests (No Hardware)

```bash
poetry run --directory tak-bridge-escpos pytest -v  # 9 tests, FakeSerial/EventBus, no pigpio/Pi
poetry run --directory tak-bridge-escpos black --check src tests && poetry run --directory tak-bridge-escpos mypy src && poetry run --directory tak-bridge-escpos pylint src
```

### 6.5 Systemd (Softserial)

```bash
sudo systemctl enable pigpiod
sudo cp tak-bridge-escpos/systemd/tak-bridge-escpos.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tak-bridge-escpos
journalctl -u tak-bridge-escpos -f
cat /tmp/tak-escpos-healthy || echo healthy
```

See `tak-bridge-escpos/README_main.md:7` for full systemd table, `pigpiod` dependency, `dialout`/`gpio` troubleshooting.

---

## 7. Library Usage (Direct)

```python
import asyncio
from tak_bridge_escpos.escpos import EscPosPrinter, PrintRequest
from takpi_common.bus import EventBus

# Direct async
async def direct():
    async with EscPosPrinter(tx_pin=27, baudrate=9600) as p:
        await p.fat("911 ALARM", align="center")
        await p.print_text("From: ALPHA", bold=True)
        await p.print_alarm(callsign="ALPHA", lat=60.1, lon=24.8, location="Helsinki, Kamppi")
        await p.cut()

# Via EventBus (main → printer)
async def via_bus():
    bus = EventBus()
    printer = EscPosPrinter(tx_pin=27, baudrate=9600)
    await printer.connect()
    bus.subscribe(PrintRequest, printer.handle_print_request)
    await bus.publish(PrintRequest(text="911 alarm received from ALPHA at 60.1,24.8 Helsinki", fat=True, cut_after=True))
    await bus.publish(PrintRequest(text="Double height", double_height=True))
```

See `tak-bridge-escpos/README_escpos.md:8` for full API (`fat`/`double_height`/`double_width`/`bold`/`set_size`/`set_align`/`feed`/`cut`/`print_qr`).

---

## 8. Service – Main Can Print (EventBus + Cot → 911)

Deep dive `tak-bridge-escpos/README_main.md:1`.

```
main process EventBus ──PrintRequest(text, fat, cut)──► printer.handle_print_request ──► EscPosPrinter._write (softserial BCM27 @baudrate or hardware /dev/serial0) ──► paper

CotStream (takstream) ──CotReceived(b-a-o-tif)──► EventBus ──► _handle_cot_print(bus, printer, cot) ──► printer.print_alarm(callsign, lat, lon, location, time, remarks) ──► cut
```

- `PrintRequest` (`escpos.py:42`): `text`/`raw_bytes`, `fat`/`bold`/`double_width`/`double_height`/`underline`/`align`/`cut_after`/`feed_lines`, `handle_print_request` routes to `print_text` or raw `_write`.
- `CotReceived` 911: `_handle_cot_print` (`__main__.py:62`) checks `is_emergency()` or `cot_type.startswith("b-a-o-tif")`, formats `_format_location` (`lat,lon` → `60.12345, 24.98765`), calls `print_alarm` + `cut`.
- Example `PrintRequest` for 911: `PrintRequest(text="911 alarm received from ALPHA at 60.1,24.8 Helsinki", fat=True, align="center", cut_after=True, feed_lines=2)`.

---

## 9. Configuration Reference (Consolidated)

| Var | Default | Docs | Notes |
|-----|---------|------|-------|
| `ESCPOS_PORT` | `/dev/serial0` or `None` if `TX_PIN` set | `README_main.md:4` | Hardware port – `/dev/ttyUSB0`, `/dev/serial0`; mutually exclusive with `TX_PIN` (softserial) |
| `ESCPOS_BAUDRATE` | `9600` | `README_main.md:4` | Must match printer DIP (9600/19200/38400/57600/115200) |
| `ESCPOS_TX_PIN` | `None` | `README_main.md:4` | BCM TX for softserial (e.g., `27` → pin13) – enables `pigpio` bit-bang |
| `ESCPOS_RX_PIN` | `None` | `README_main.md:4` | BCM RX for softserial (e.g., `22` → pin15) – optional |
| `ESCPOS_ENCODING` | `cp437` | `README_escpos.md:4` | `cp437` or `utf8` |
| `ESCPOS_FAKE` | `0` | `README_main.md:5` | `1` → `FakeSerial` (CI) |
| `TAK_HOST`/`TAK_PORT` | `None` | `README_main.md:4` | If set, `CotBus` auto-prints 911 via `print_alarm` |
| `HEALTH_FILE` | `/tmp/tak-escpos-healthy` | `__main__.py:1` | Health flag |

---

## 10. File Map

```
takpi/
  README.md                         # index (now 11 README_<topic>.md)
  README_escpos.md                  # this file (top-level escpos aggregation)
  common/                           # shared EventBus + takstream CotBus
  tak-bridge-escpos/
    README.md                       # bridge overview (index)
    README_escpos.md                # driver deep dive (softserial GPIO, baudrate, ESC/POS bytes, fat)
    README_main.md                  # service deep dive (PrintRequest→printer, Cot 911→print, examples)
    pyproject.toml:1                # deps pyserial/pigpio/takpi-common/takstream develop
    src/tak_bridge_escpos/
      __init__.py:1                 # re-exports EscPosPrinter, PrintRequest
      escpos.py:1                   # driver (FakeSerial/SoftSerial/HardwareSerial, fat/double, print_alarm, handle_print_request)
      __main__.py:1                 # service (EventBus, CotReceived→print_alarm, softserial vs port, health)
    tests/
      test_escpos.py:1              # 9 tests (FakeSerial, fat, alarm, PrintRequest via bus, Cot→print)
    systemd/
      tak-bridge-escpos.service:1   # softserial pigpiod dep, /tmp/tak-escpos-healthy
```

---

## 11. Development

```bash
poetry install --directory tak-bridge-escpos  # also installs takpi-common + takstream develop
poetry run --directory tak-bridge-escpos black . && poetry run --directory tak-bridge-escpos mypy src && poetry run --directory tak-bridge-escpos pylint src
poetry run --directory tak-bridge-escpos pytest -v  # FakeSerial, no Pi/pigpio
```

`pigpio` is optional – tests use `FakeSerial`/`FakePigpio`, no daemon needed. For real softserial on Pi, `sudo pigpiod` must run and user in `gpio`.

---

## 12. Troubleshooting (Condensed)

| Problem | Check | Fix |
|---------|-------|-----|
| `SerialException: could not open port` | Wrong `ESCPOS_PORT` or permission | `ls -l /dev/serial0`/`/dev/ttyUSB0`, `sudo usermod -a -G dialout,gpio $USER`, `pigpiod` running if softserial |
| Garbage print | Baud mismatch | Match `ESCPOS_BAUDRATE` to printer DIP (9600 vs 19200) – try 9600 first |
| Nothing prints on softserial | `tx_pin` BCM wrong or pigpio not running | `pigs pigs`? `sudo pigpiod`, check `tx_pin` BCM number (not WiringPi), scope BCM27 pin13 @baudrate with logic analyzer, `pigpiod` logs `journalctl -u pigpiod` |
| Fat not fat | Printer doesn't support `GS !` | Most do; try `double_height` alone; check `encoding` `cp437` |
| 911 not auto-printing | `TAK_HOST` not set or `is_emergency` false | Set `TAK_HOST`/`PORT`/`CERT` in `.env`, check `cot_type` `b-a-o-tif`, logs `CoT received b-a-o-tif -> LED` vs `... -> print` |
| Health file present | Print failed 3× | `journalctl -u tak-bridge-escpos`, check `PrintRequest` handler exception, `pigpiod` or `port` |

Full matrix `tak-bridge-escpos/README_main.md:8` + `README_escpos.md:11`.

---

## 13. Changelog & Future

| Version | Date | Notes |
|---------|------|-------|
| 0.1.0 | 2026-09-07 | Initial: async `EscPosPrinter` softserial BCM `tx_pin`/`rx_pin` + hardware `port`, configurable `baudrate` 9600-115200, `fat`/`double_height`/`double_width`/`bold`/`print_alarm`/`cut`/`PrintRequest` on `EventBus`, Cot 911 `print_alarm` via `CotBus` (takstream), `FakeSerial`/`FakePigpio` tests, systemd `pigpiod` |

**Future:** QR via `GS ( k`, barcode `GS k`, image `GS v 0`, status `DLE EOT`, paper sensors, `common` `PrintBus` helper.

---

## 14. References

- Bridge: `tak-bridge-escpos/README.md:1`
- Driver: `tak-bridge-escpos/README_escpos.md:1` + `src/tak_bridge_escpos/escpos.py:1` (`FakeSerial`/`SoftSerial`/`EscPosPrinter`)
- Service: `tak-bridge-escpos/README_main.md:1` + `src/tak_bridge_escpos/__main__.py:1` (`PrintRequest`/`CotReceived`→`print_alarm`)
- Tests: `tak-bridge-escpos/tests/test_escpos.py:1`
- Bus: `takpi_common/bus.py:1` (`EventBus`, `PrintRequest`), `takpi_common/cot_bus.py:1` (`CotBus`)
- Takstream: `python-tak-cot-streaming/README.md:1`, `src/takstream/cot.py:1` (`CotEvent.is_emergency`), `src/takstream/stream.py:1` (`CotStream`)
- Common: `common/README_bus.md:1`, `common/README_cot.md:1`, `common/README_app.md:1`
- Monorepo: `README.md:1` (11 docs), `AGENTS.md:1` (takstream, MCP23017, EventBus)
