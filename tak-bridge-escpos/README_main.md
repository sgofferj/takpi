# README_main – ESC/POS Service Submodule (EventBus + 911 Alarm Print)

> **Path:** `tak-bridge-escpos/src/tak_bridge_escpos/__main__.py:1` (`main`, `run`, `PrintRequest` handling, `CotReceived` → `print_alarm`)  
> **Driver:** `tak-bridge-escpos/README_escpos.md:1` (`escpos.py:1` `EscPosPrinter` softserial BCM `tx_pin`/`rx_pin` + `baudrate`, fat/double, `FakeSerial`)  
> **Bridge:** `tak-bridge-escpos/README.md:1`, Top-level `../../README_escpos.md:1`  
> **Common bus:** `../../common/src/takpi_common/bus.py:1` (`EventBus`), `../../common/src/takpi_common/cot_bus.py:1` (`CotBus`, `CotReceived`/`CotSend`, takstream)

This is the thorough reference for the **main service submodule** that lets the takpi main process print via `EventBus` (`PrintRequest` → printer) and **automatically prints 911 alarms** (`CotReceived` `b-a-o-tif` from `takstream.CotStream` via `CotBus` → `print_alarm("911 alarm received from <callsign> at <location>")`).

---

## 1. Purpose – Main Can Print, e.g., 911 Alarm

**Requirement:** *"Main process can print stuff, e.g. `911 alarm received from <callsign> at <location>`"* – with **softserial GPIO**, **configurable baudrate/pins**, and **fat/double** styles.

The service provides **two equivalent paths** for main to print (both via `EscPosPrinter` on `EventBus`):

1. **Direct `PrintRequest` on `EventBus`** – any takpi component (button handler, timer, API) can publish `PrintRequest(text="...", fat=True, cut_after=True)` and printer handles it (`handle_print_request`).

2. **Automatic 911 alarm via CoT** – when `CotStream` (takstream, not pytak, via `CotBus`) receives `b-a-o-tif` emergency (`CotEvent.is_emergency()`), service's `_handle_cot_print` calls `printer.print_alarm(callsign, lat, lon, location, time, remarks)` + `cut`, producing paper like:

```
*** 911 ALARM *** (fat, centered)
From: ALPHA (bold)
Loc:  60.12345, 24.98765
      Helsinki
Time: 2026-09-07 12:34:56 UTC
Type: b-a-o-tif
Remarks: car crash
--------------------------------
```

Both use the same `EscPosPrinter` (softserial `tx_pin`/`rx_pin` or hardware `port`, configurable `baudrate`, fat/double).

---

## 2. Architecture – EventBus + CotBus + Printer

```
Main takpi process (TakPiApp or any component)
   │   ┌───────────────────────────────────┐
   ├───►│ EventBus (takpi_common/bus.py:1) │◄───┐
   │    │  PrintRequest                   │    │
   │    └──────────────┬──────────────────┘    │
   │                   │ handle_print_request  │
   │                   ▼                       │
   │         ┌─────────────────────┐           │
   │         │ EscPosPrinter       │           │
   │         │ escpos.py:1         │           │
   │         │  softserial BCM      │           │
   │         │  tx_pin=27 @9600     │           │
   │         │  via pigpio          │           │
   │         │  or hardware port    │           │
   │         └─────────┬───────────┘           │
   │                   │ TTL serial            │
   │                   ▼                       │
   │              [Thermal Printer]            │
   │                                           │
   │    CotStream (takstream) ──CotReceived──►EventBus ──► _handle_cot_print ──► printer.print_alarm ──► cut
   │    (takpi/python-tak-cot-streaming)      (b-a-o-tif)   (callsign, lat/lon, location, time)
```

**Flow:**

- `PrintRequest` (`escpos.py:42`): `text`/`raw_bytes` + style flags (`fat`, `bold`, `double_width`, `double_height`, `underline`, `align`, `cut_after`, `feed_lines`) – published by main on `EventBus`, handled by `printer.handle_print_request` (`__main__.py:on_print`) which routes to `printer.print_text` or raw `_write` + optional `cut`.

- `CotReceived` (`cot_bus.py:15`): `CotBus` (bridging `takstream.CotStream` ↔ `EventBus`) publishes each inbound `CotEvent`; `__main__.py:_handle_cot_print` checks `is_emergency()` or `cot_type` `b-a-o-tif`, calls `printer.print_alarm(...)` with `_format_location` (`lat,lon` → `60.12345, 24.98765`) and `cut`.

**Decoupling:** Main never imports `EscPosPrinter` directly for 911 – it just publishes `PrintRequest` or relies on `_handle_cot_print` subscription; printer never imports `CotEvent` except via bus.

---

## 3. Service Code (`__main__.py:1`)

### 3.1 Key Functions

| Function | Line | Role |
|----------|------|------|
| `_printer_from_env()` | `__main__.py:19` | Reads `ESCPOS_PORT`/`ESCPOS_TX_PIN`/`ESCPOS_RX_PIN`/`ESCPOS_BAUDRATE` env → `EscPosPrinter` (softserial if `tx_pin` set else hardware `port`; logs `softserial BCM TX=...` or `hardware serial ...`) |
| `_handle_print_request(printer, req)` | `__main__.py:36` | `PrintRequest` handler – `await printer.handle_print_request(req)`, `_report(True)` or `False` on exception |
| `_format_location(cot)` | `__main__.py:45` | `lat,lon` → `"60.12345, 24.98765"` or `"unknown"` |
| `_handle_cot_print(bus, printer, cot)` | `__main__.py:52` | `CotReceived` handler – checks `is_emergency()`/`b-a-o-tif`, calls `printer.print_alarm(callsign, lat, lon, location, time, remarks, cot_type)` + `cut`, logs `911 alarm from <callsign> at <location> – printing` |
| `main()` | `__main__.py:85` | `load_dotenv`, `HEALTH_FILE` override, `log_level` DEBUG/INFO, `bus=EventBus()`, `printer=_printer_from_env()` (+ `ESCPOS_FAKE=1` → `FakeSerial`), `await printer.connect()`, `bus.subscribe(PrintRequest, on_print)`, optional `CotBus`+`CotStream` if `TAK_HOST`/`TAK_PORT` set (`bus.subscribe(CotReceived, on_cot)`), graceful `SIGINT`/`SIGTERM` `stop_event`, `while not stop_event.is_set(): sleep(1)` health tick |
| `run()` | `__main__.py:185` | Sync wrapper `asyncio.run(main())` for `poetry run start` |

### 3.2 Startup Sequence

```
main()
 ├─ load_dotenv, HEALTH_FILE, log_level
 ├─ bus = EventBus()
 ├─ printer = _printer_from_env()  # tx_pin=27 @9600 or port=/dev/ttyUSB0
 ├─ if ESCPOS_FAKE=1: printer = EscPosPrinter(serial_instance=FakeSerial())
 ├─ await printer.connect()  # HardwareSerial (pyserial) or SoftSerial (pigpio wave) + init ESC @
 ├─ bus.subscribe(PrintRequest, on_print)  # main → printer
 ├─ if TAK_HOST+TAK_PORT:
 │    ├─ from takstream import CotStream; from takpi_common.cot_bus import CotBus
 │    ├─ stream = await CotStream.connect(host,port,cert,key,ca,callsign,team,role)
 │    ├─ cot_bus = CotBus(bus, stream); await cot_bus.start()
 │    └─ bus.subscribe(CotReceived, on_cot)  # CotReceived → _handle_cot_print → print_alarm
 ├─ loop.add_signal_handler(SIGINT/SIGTERM → stop_event)
 └─ while not stop_event.is_set(): await sleep(1)  # keep alive, health tick
    → on stop: await cot_bus.stop(); await stream.close(); await printer.disconnect()
```

---

## 4. Configuration (Env / .env)

All via `python-dotenv` `load_dotenv()` `__main__.py:86`.

| Var | Default | Source Line | Description |
|-----|---------|-------------|-------------|
| `ESCPOS_PORT` / `PRINTER_PORT` | `None` if `TX_PIN` set else `/dev/serial0` fallback in `_printer_from_env` | `__main__.py:20` | Hardware port – `/dev/serial0` (GPIO14/15 UART, share with chronometer? Use softserial if so), `/dev/ttyUSB0` (USB-TTL dongle), `/dev/serial/by-id/...` – **mutually exclusive with `TX_PIN`** |
| `ESCPOS_BAUDRATE` / `PRINTER_BAUDRATE` | `9600` | `__main__.py:21` | **Configurable** 9600|19200|38400|57600|115200 – **must match printer DIP** (9600 default) |
| `ESCPOS_TX_PIN` / `PRINTER_TX_PIN` | `None` | `__main__.py:22` | **BCM TX pin** for softserial (e.g., `27` → header pin13 → printer RX via `pigpio` bit-bang). If set, softserial mode, `port` ignored. |
| `ESCPOS_RX_PIN` / `PRINTER_RX_PIN` | `None` | `__main__.py:23` | **BCM RX pin** for softserial (e.g., `22` → pin15 ← printer TX, optional status). |
| `ESCPOS_ENCODING` | `cp437` | `__main__.py:24` | `cp437` (Epson USA) or `utf8` |
| `ESCPOS_FAKE` | `0` | `__main__.py:104` | `1` → `FakeSerial` injected (CI, no hardware/pigpio) |
| `TAK_HOST` / `API_HOST` | `None` | `__main__.py:140` | If set, service auto-connects `CotStream` (takstream) for 911 auto-print; else `PrintRequest` only |
| `TAK_PORT` / `API_PORT` | `8089` (if TAK_HOST set) | `__main__.py:141` | TAK streaming port (8089 TLS, 8087 plaintext) |
| `TAK_CERT` / `CLIENT_CERT` | `None` | `__main__.py:159` | PEM client cert for mTLS |
| `TAK_KEY` / `CLIENT_KEY` | `None` | `__main__.py:160` | PEM private key |
| `TAK_CA` | `None` | `__main__.py:161` | CA for server verification (optional, TAK self-signed) |
| `TAK_CALLSIGN` / `MY_UID` | `PI-PRINTER` | `__main__.py:162` | Own callsign for `CotStream` identity SA |
| `TAK_TEAM`/`TAK_ROLE` | `Cyan`/`Team Member` | `__main__.py:163` | Team/role for identity |
| `HEALTH_FILE` | `/tmp/tak-escpos-healthy` | `__main__.py:16` | Health flag (absent=healthy, `HEALTH_MAX_ERRORS=3` pattern as chronometer) |
| `DEBUG` | `0` | `__main__.py:93` | `1` → DEBUG |

**Precedence:** `ESCPOS_TX_PIN` present ⇒ softserial (pigpio) even if `ESCPOS_PORT` also set (warns); else hardware `port`.

### 4.1 `.env` Examples

**Softserial GPIO (recommended when `/dev/serial0` used by chronometer):**

```
ESCPOS_TX_PIN=27
ESCPOS_RX_PIN=22
ESCPOS_BAUDRATE=9600
ESCPOS_ENCODING=cp437
```

**Hardware UART/USB:**

```
ESCPOS_PORT=/dev/serial0
ESCPOS_BAUDRATE=19200
# or USB dongle stable:
ESCPOS_PORT=/dev/serial/by-id/usb-FTDI_FT232R_...-if00-port0
ESCPOS_BAUDRATE=115200
```

**With TAK 911 auto-print (softserial + CotStream):**

```
ESCPOS_TX_PIN=27
ESCPOS_BAUDRATE=9600
TAK_HOST=tak.example.com
TAK_PORT=8089
TAK_CERT=certs/client.pem
TAK_KEY=certs/client.key
TAK_CALLSIGN=PI-PRINTER
TAK_TEAM=Cyan
TAK_ROLE=Team Member
```

**Bench CI (no hardware, no pigpio, no TAK):**

```
ESCPOS_FAKE=1
```

**File location:** `tak-bridge-escpos/.env` (gitignored, `EnvironmentFile=` in systemd). Systemd loads it via `EnvironmentFile=/home/pi/Dev/TAK/takpi/tak-bridge-escpos/.env`.

---

## 5. Usage Examples (Main Process Can Print)

### 5.1 Direct `PrintRequest` on `EventBus` (Any Component → Printer)

```python
import asyncio
from takpi_common.bus import EventBus
from tak_bridge_escpos.escpos import EscPosPrinter, PrintRequest

async def main():
    bus = EventBus()
    # Softserial GPIO 27 @9600 (or hardware port, or FakeSerial for CI)
    printer = EscPosPrinter(tx_pin=27, baudrate=9600)  # or port="/dev/ttyUSB0"
    await printer.connect()
    bus.subscribe(PrintRequest, printer.handle_print_request)

    # Main process can print from anywhere (button handler, timer, HTTP, etc.):
    await bus.publish(PrintRequest(text="Hello", fat=True, align="center", cut_after=True, feed_lines=2))
    await bus.publish(PrintRequest(text="911 alarm received from ALPHA at 60.1,24.8 Helsinki", fat=True, align="left"))
    await bus.publish(PrintRequest(text="Double height line", double_height=True))
    # Raw bytes (pre-formatted ESC/POS)
    await bus.publish(PrintRequest(raw_bytes=b"\x1b@\x1bE\x01Bold raw\n", cut_after=True))

asyncio.run(main())
```

**Convenience styles via `PrintRequest` flags:** `fat` (bold+double), `bold`, `double_width`, `double_height`, `underline`, `align` left/center/right, `cut_after`, `feed_lines`. `handle_print_request` (`escpos.py:285`) routes to `print_text` with auto-reset.

### 5.2 Direct Printer Methods (No Bus)

```python
import asyncio
from tak_bridge_escpos.escpos import EscPosPrinter

async def main():
    async with EscPosPrinter(tx_pin=27, baudrate=9600) as p:
        await p.fat("911 ALARM", align="center")
        await p.double_height("Double height")
        await p.print_text("Bold left", bold=True, align="left")
        await p.print_alarm(callsign="ALPHA", lat=60.1, lon=24.8, location="Helsinki", remarks="car crash")
        await p.cut()

asyncio.run(main())
```

### 5.3 911 Alarm Auto-Print via Cot (Main → Printer via `CotReceived`)

```python
import asyncio
from takpi_common.bus import EventBus
from takpi_common.cot_bus import CotReceived
from tak_bridge_escpos.escpos import EscPosPrinter

# In main TalPiApp or __main__.py _handle_cot_print:
async def _handle_cot_print(bus, printer, cot):
    # cot is takstream.CotEvent
    is_em = cot.is_emergency() if hasattr(cot, "is_emergency") else str(getattr(cot, "cot_type","")).startswith("b-a-o-tif")
    if not is_em:
        return
    callsign = getattr(cot, "callsign", "unknown")
    lat = getattr(cot, "lat", None)
    lon = getattr(cot, "lon", None)
    # Option A: direct printer call (if printer in closure)
    await printer.print_alarm(callsign=str(callsign), lat=lat, lon=lon, location=None)
    await printer.cut()
    # Option B: via bus (decoupled, testable) – publish PrintRequest with alarm text
    # await bus.publish(PrintRequest(text=f"911 alarm received from {callsign} at {lat},{lon}", fat=True))

# Wiring in main:
# bus.subscribe(CotReceived, lambda e: _handle_cot_print(bus, printer, e.cot))
# CotBus publishes CotReceived for each inbound CotStream event (see common/README_cot.md:1)
```

**`__main__.py:62` real handler** does exactly this: `CotReceived` with `b-a-o-tif` → `_handle_cot_print` → `_format_location` → `printer.print_alarm` + `cut`, logs `911 alarm from <callsign> at <location> – printing`.

### 5.4 Softserial GPIO + Baudrate Config

```python
# 9600 is safest for softserial bit-bang (pigpio timing) – use for 58mm printers
p = EscPosPrinter(tx_pin=17, baudrate=9600)  # BCM17 pin11 → printer RX
# 19200 also works if pigpio daemon not loaded heavily (e.g., no I2C contention)
p = EscPosPrinter(tx_pin=27, rx_pin=22, baudrate=19200)
# Hardware fallback for bench (USB-TTL dongle, no pigpio daemon needed)
p = EscPosPrinter(port="/dev/serial/by-id/usb-FTDI_...", baudrate=115200)
# Invalid: neither port nor tx_pin → ValueError
# Both given → tx_pin (softserial) takes precedence with warning
```

---

## 6. Running

### 6.1 Prerequisites (Pi)

```bash
# For softserial GPIO (Tx pin set) – pigpio daemon must run
sudo pigpiod
sudo systemctl enable pigpiod
# For hardware serial port /dev/serial0 – enable UART if not using softserial (see chronometer docs)
# Permissions for both modes
sudo usermod -a -G dialout,gpio $USER; newgrp dialout
# Verify
ls -l /dev/serial0  # hardware
pigs pigs  # pigpio test
gpioinfo | grep -E "27|22"
poetry install --directory tak-bridge-escpos  # pulls takpi-common + takstream path develop, pyserial, pigpio optional
```

### 6.2 Install & Run Foreground

```bash
# Softserial (BCM27) – recommended when /dev/serial0 used by chronometer
ESCPOS_TX_PIN=27 ESCPOS_BAUDRATE=9600 poetry run --directory tak-bridge-escpos start
# Hardware UART/USB
ESCPOS_PORT=/dev/ttyUSB0 ESCPOS_BAUDRATE=19200 poetry run --directory tak-bridge-escpos start
# Fake (CI, no hardware/pigpio/TAK)
ESCPOS_FAKE=1 poetry run --directory tak-bridge-escpos start
# With TAK 911 auto-print
TAK_HOST=tak.example.com TAK_PORT=8089 ESCPOS_TX_PIN=27 poetry run --directory tak-bridge-escpos start
```

Expected log:

```
INFO EscPos softserial opened BCM TX=27 RX=22 @9600
INFO Subscribed PrintRequest → printer (softserial=True)
INFO CotBus started ... / CoT 911 auto-print subscribed (if TAK_HOST set)
INFO tak-bridge-escpos ready – publish PrintRequest on EventBus or send 911 CoT to print
```

Trigger print: publish `PrintRequest` on bus (e.g., via `panel_demo` button) or send `b-a-o-tif` CoT from TAK (e.g., `CotEvent.emergency_alert`).

### 6.3 Health Check

```bash
cat /tmp/tak-escpos-healthy  # no file = healthy (per health.py:1, same as chronometer)
# After 3 consecutive print failures (pigpio not running, port denied, baud mismatch) → file appears
```

### 6.4 Without Hardware (CI)

```bash
poetry run --directory tak-bridge-escpos pytest -v  # 9 tests, FakeSerial/FakePigpio, no Pi/pigpio/TAK
poetry run --directory tak-bridge-escpos python -c "from tak_bridge_escpos.escpos import EscPosPrinter, FakeSerial; import asyncio; asyncio.run(EscPosPrinter(serial_instance=FakeSerial()).print_alarm(callsign='T'))"
```

---

## 7. Systemd Deployment (Pi, Softserial vs Hardware)

**Unit file:** `systemd/tak-bridge-escpos.service:1` (softserial needs `pigpiod`):

```ini
[Unit]
Description=TAK Bridge – ESC/POS Thermal Printer (softserial GPIO or hardware serial, fat/double, 911 alarm)
After=network.target pigpiod.service
Wants=network.target pigpiod.service

[Service]
Type=simple
User=pi
Group=dialout
SupplementaryGroups=gpio
WorkingDirectory=/home/pi/Dev/TAK/takpi/tak-bridge-escpos
EnvironmentFile=/home/pi/Dev/TAK/takpi/tak-bridge-escpos/.env  # ESCPOS_TX_PIN=27 +/ TAK_HOST
ExecStart=/home/pi/.local/bin/poetry run start
Restart=always
RestartSec=5
ExecStartPre=/bin/sh -c 'rm -f /tmp/tak-escpos-healthy'

[Install]
WantedBy=multi-user.target
```

### 7.1 Install Steps

```bash
# 0. Enable pigpiod for softserial (if ESCPOS_TX_PIN set)
sudo systemctl enable --now pigpiod
systemctl status pigpiod
# 1. Ensure .env with ESCPOS_TX_PIN or ESCPOS_PORT + TAK_HOST if 911 needed
cat tak-bridge-escpos/.env  # ESCPOS_TX_PIN=27, ESCPOS_BAUDRATE=9600
# 2. Install service
sudo cp tak-bridge-escpos/systemd/tak-bridge-escpos.service /etc/systemd/system/
sudo systemctl daemon-reload
# 3. Enable & start
sudo systemctl enable --now tak-bridge-escpos
# 4. Verify
systemctl status tak-bridge-escpos
journalctl -u tak-bridge-escpos -f
cat /tmp/tak-escpos-healthy || echo healthy
# 5. Test print
echo "test" | poetry run --directory tak-bridge-escpos python -c "import asyncio; from tak_bridge_escpos.escpos import EscPosPrinter; asyncio.run(EscPosPrinter(tx_pin=27, baudrate=9600).print_text('test'))"
```

### 7.2 Common Pitfalls

| Error | Cause | Fix |
|-------|-------|-----|
| `ValueError: Must provide port or tx_pin` | No `ESCPOS_PORT` nor `TX_PIN` in `.env` | Set `ESCPOS_TX_PIN=27` (softserial) or `ESCPOS_PORT=/dev/ttyUSB0` (hardware) |
| `pigpio daemon not running` warning + `FakePigpio` | `pigpiod` not running for softserial | `sudo pigpiod; sudo systemctl enable pigpiod`; for CI, `ESCPOS_FAKE=1` or inject `FakePigpio` |
| `SerialException: [Errno 13] Permission denied /dev/serial0` | Not in `dialout`/`gpio` | `sudo usermod -a -G dialout,gpio pi`; `ls -l /dev/serial0`, `groups` |
| Garbage print | Baud mismatch | Match `ESCPOS_BAUDRATE` to printer DIP (try 9600, then 19200...), check `pigpio` timing (softserial 9600 safest), scope BCM27 pin13 @baudrate |
| Nothing on softserial GPIO | Wrong BCM number (WiringPi != BCM) or pigpio not running or printer RX not connected | Use BCM (`27`=pin13, `22`=pin15, `17`=pin11); `gpioinfo | grep 27`; `pigs pigs`; check wire BCM27→printer RX, common GND |
| `PrintRequest` not printing | No subscriber or wrong `id` | `bus.subscribe(PrintRequest, printer.handle_print_request)` must happen after `await printer.connect()`; check `bus.subscribed(PrintRequest)` |
| 911 not auto-printing | `TAK_HOST` not set or `is_emergency` false or `cot_type` not `b-a-o-tif` | Set `TAK_HOST`/`PORT`/`CERT` in `.env`, check `cot_type` `b-a-o-tif`, logs `911 alarm from ... – printing` |
| Health file present | 3 consecutive print failures | `journalctl -u tak-bridge-escpos`, check `PrintRequest` handler exception, `pigpiod` or `port` |

### 7.3 Monitoring

```bash
watch -n 1 cat /tmp/tak-escpos-healthy || echo healthy
systemctl show tak-bridge-escpos --property=ActiveEnterTimestamp --property=NRestarts
journalctl -u tak-bridge-escpos --since "1 hour ago" | grep -E "PrintRequest|911|softserial|hardware"
# pigpio
systemctl status pigpiod; journalctl -u pigpiod -f
# Check UART if hardware
ls -l /dev/serial0 && stty -F /dev/serial0 -a
```

---

## 8. Testing

| Test | Coverage |
|------|----------|
| `test_fake_serial_basic` | `FakeSerial` `ESC @` |
| `test_softserial_fake_via_injection` | `tx_pin`/`rx_pin` + `FakePigpio` injection, `is_softserial` |
| `test_convenience_fat_double` | `fat` → `ESC E`+`GS ! 0x11`, `double_height`/`double_width`/`bold` |
| `test_print_text_styles_and_align` | `print_text` bold+underline+center, `baudrate` 19200 |
| `test_print_alarm_contains_callsign_and_location` | `print_alarm` contains `911 ALARM`, callsign, lat/lon, location, remarks, `b-a-o-tif` |
| `test_print_request_via_bus` | `EventBus` `PrintRequest` (fat, cut) → `FakeSerial` bytes + `GS V` |
| `test_cot_911_auto_print_via_bus` | `CotReceived` emergency → `print_alarm` via `EventBus` handler |
| `test_configurable_baudrate_and_gpio_pins` | `port`/`baudrate` vs `tx_pin`/`rx_pin`/`baudrate`, `is_softserial`, invalid config |
| `test_header_only` | `ValueError` when no port/tx_pin/instance |

Run:

```bash
poetry run --directory tak-bridge-escpos pytest -v  # 9 tests
poetry run --directory tak-bridge-escpos black --check src tests && poetry run --directory tak-bridge-escpos mypy src && poetry run --directory tak-bridge-escpos pylint src
```

All with `FakeSerial`/`FakePigpio`/`AsyncMock` – no Pi/pigpio/printer/TAK.

---

## 9. Future Extensions

- **QR/barcode:** `print_qr` `GS ( k` (model 2) + `GS k` barcode (needs printer model quirks) – placeholder now prints text QR.
- **Images:** `GS v 0` raster for logos/maps via `Pillow`.
- **Status:** `DLE EOT` paper sensor, `GS a` busy, `ESC c` panel buttons.
- **Common `PrintBus`:** Move `PrintRequest` to `takpi_common` if multiple printers (e.g., 58mm + 80mm) share bus.
- **Docker:** `--device=/dev/serial0` or `--device=/dev/gpiochip0` + `pigpiod` host.

---

## 10. References

- Driver: `tak-bridge-escpos/README_escpos.md:1` (`escpos.py:1` `EscPosPrinter`, `FakeSerial`/`SoftSerial`, `PrintRequest`, `fat`/`print_alarm`)
- Bridge overview: `README.md:1`
- Top-level: `../../README_escpos.md:1` (`softserial GPIO`/`baudrate`/`fat`/`911 alarm`)
- Bus: `../../common/src/takpi_common/bus.py:1` (`EventBus`, `PrintRequest`, `CotReceived`), `../../common/src/takpi_common/cot_bus.py:1` (`CotBus` takstream)
- Takstream: `../../python-tak-cot-streaming/README.md:1`, `../../python-tak-cot-streaming/src/takstream/cot.py:1` (`CotEvent.is_emergency`), `../../python-tak-cot-streaming/src/takstream/stream.py:1` (`CotStream`)
- Common: `../../common/README_bus.md:1`, `../../common/README_cot.md:1`, `../../common/README_app.md:1` (TakPiApp)
- Health: `../../common/src/takpi_common/health.py:1`, `tak-bridge-chronometer` (GPIO UART contrast, `EventBus` pattern)
- Tests: `tests/test_escpos.py:1` (9 tests)

---

## 11. Changelog

| Version | Date | Notes |
|---------|------|-------|
| 0.1.0 | 2026-09-07 | Initial async ESC/POS – softserial BCM `tx_pin`/`rx_pin` + hardware `port`, configurable `baudrate` 9600-115200, `fat`/`double_height`/`double_width`/`bold`/`print_alarm` (911) + `PrintRequest` on `EventBus`, Cot 911 `print_alarm` via `CotBus` (takstream), `FakeSerial`/`FakePigpio` tests, systemd `pigpiod` |

