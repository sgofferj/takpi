# README_escpos – ESC/POS Driver Submodule (Softserial GPIO, Baudrate, Fat)

> **Path:** `tak-bridge-escpos/src/tak_bridge_escpos/escpos.py:1` (`EscPosPrinter`, `FakeSerial`, `SoftSerial`, `HardwareSerial`, `PrintRequest`)  
> **Service:** `README_main.md:1` (`__main__.py:1` → `PrintRequest`/`CotReceived` → print)  
> **Bridge:** `README.md:1`, Top-level `../../README_escpos.md:1`  
> **Common bus:** `../../common/src/takpi_common/bus.py:1` (`EventBus`)

This is the thorough reference for the **ESCPOS driver submodule** – async, **configurable baudrate** (9600-115200), **softserial GPIO** (`tx_pin`/`rx_pin` BCM via `pigpio` bit-bang) vs hardware serial (`/dev/serial0`/`/dev/ttyUSB0` via `pyserial`), with convenience styles **fat** (bold+double), double-height/width, etc., and `print_alarm` for main.

---

## 1. Purpose & Scope

Printer bridges need **no TAK UART conflict** – chronometer already uses `/dev/serial0` GPIO14/15 hardware UART. **Softserial GPIO** solves it: printer on spare BCM pins (e.g. `BCM27` pin13 TX) via `pigpio` daemon bit-bang at any baudrate, while hardware UART stays free.

**Goals**
- One async driver for both modes: softserial (BCM pins + baudrate + `pigpio`) and hardware (port + baudrate + `pyserial`)
- **Configurable baudrate** `9600|19200|38400|57600|115200` (DIP must match) + **configurable GPIO pins** `tx_pin`/`rx_pin` (BCM numbers, not WiringPi/physical)
- **Async** (`asyncio.Lock` + `run_in_executor` for sync `write`, `0.005 s` gap) – non-blocking for main takpi process
- Convenience **fat** (`GS ! 0x11` + `ESC E`) / double-height (`GS ! 0x01`) / double-width (`0x10`) / bold / underline / align / feed / cut / `print_alarm` (911)
- `PrintRequest` on `EventBus` so **main process can print** from anywhere (`await bus.publish(PrintRequest(text=..., fat=True))`), and `print_alarm` helper prints `911 alarm received from <callsign> at <location>` with fat header
- Testable without Pi/pigpio/hardware via `FakeSerial`/`FakePigpio` + `AsyncMock`

**Not:** Full `python-escpos` (sync, no softserial, no async, no EventBus). Minimal ESC/POS (init, size, bold, align, feed, cut, codepage) – enough for 911 paper, extensible to QR/barcode later.

---

## 2. Hardware Reference

| Item | Value |
|------|-------|
| Printers | Epson TM-T20-II, Adafruit Mini Thermal 58mm, Xprinter XP-N160I, Goojprt PT-210, generic 58mm/80mm TTL ESC/POS (5-9V PSU, 1.5-2A) |
| Interface | **TTL serial** 3.3V/5V (printer RX ← Pi TX; TX → Pi RX optional for status). Two modes: <br> **(A) Softserial GPIO** – `tx_pin` BCM (e.g. 27 → pin13) via `pigpio` bit-bang, `rx_pin` BCM 22 optional, `baudrate` configurable <br> **(B) Hardware serial** – `port` `/dev/serial0` (GPIO14/15 UART) or `/dev/ttyUSB0` (USB-TTL dongle) via `pyserial`, `baudrate` |
| Baudrate | **Configurable** 9600 (default, most printers DIP), 19200, 38400, 57600, 115200 – **must match printer DIP** (switches 7/8 on Epson) |
| Pins | **Configurable** `tx_pin`/`rx_pin` **BCM numbers** (e.g. `27`=pin13, `22`=pin15, `17`=pin11, `4`=pin7) – not physical pin numbers. Use `pinout` or `gpio readall`. |
| Paper | 58mm thermal roll, 32 chars/line Font A normal (42 Font B), cut via `GS V` |
| Power | External 5-9V 2A PSU (not Pi 5V header – insufficient, noise); share GND (Pi pin6 ↔ printer GND); decouple 100µF at printer |
| Daemon | Softserial requires `pigpiod` (`sudo pigpiod`, `sudo systemctl enable pigpiod`, `gpio` group) – `pigpio.pi().wave_add_serial` + `wave_send_once` for TX, `bb_serial_read_open` for RX |

### 2.1 Wiring – Softserial GPIO (Recommended When Hardware UART Busy)

```
Pi BCM27 (pin13, GPIO27) ───────► Printer RX (TTL, yellow wire)  @9600-115200 softserial via pigpio
Pi BCM22 (pin15, GPIO22) ◄─────── Printer TX (green, optional, status; 5V→3.3V divider if printer 5V)
Pi GND (pin6/9/14/20) ─────────── Printer GND (black) – common

Printer VCC 5-9V ─── External PSU 5V2A (barrel jack) – not Pi 5V pin
Printer has 3 wires only (RX, GND, VCC) for most – TX is rarely used (busy/paper sensor)
```

**No pull-ups needed** – printer RX has internal pull-up; `pigpio` sets `OUTPUT`/`INPUT`.

**Enable softserial:**

```bash
sudo pigpiod  # daemon must run before EscPosPrinter.connect()
sudo systemctl enable pigpiod
# Verify BCM pins free: not used by I2C (GPIO2/3), SPI, or chronometer (GPIO14/15)
gpioinfo | grep -E "27|22"
# Test BCM27 softserial @9600 with logic analyzer on pin13
```

**Alternative – Hardware UART:**

```
Pi pin8 GPIO14 TXD (/dev/serial0) ──► Printer RX  (when /dev/serial0 free – chronometer not using)
Pi pin10 GPIO15 RXD ──← Printer TX (optional)
Pi GND ── Printer GND
Port: /dev/serial0 @baudrate (stable symlink → /dev/ttyAMA0 Pi4/5 or /dev/ttyS0) or /dev/ttyUSB0 via USB-TTL dongle (/dev/serial/by-id)
```

Enable hardware UART per `tak-bridge-chronometer/README_chronometer.md:2.1` (`raspi-config` → Serial → No login shell / Yes hardware, `enable_uart=1`, remove `console=serial0`, `systemctl disable serial-getty@serial0`).

**Choose:** If `tak-bridge-chronometer` uses `/dev/serial0` (it does, GPIO14/15), **use softserial** for printer (e.g. `tx_pin=27`) to avoid conflict. If chronometer not present, either works.

**Level shifter:** Pi GPIO 3.3V → printer 5V often works (printer RX is 5V tolerant with threshold ~2V). If printer TX is 5V push-pull, divide to 3.3V for Pi RX (1k/2k) or use TX-only (set `rx_pin=None`).

### 2.2 Baudrate DIP Example (Epson TM-T20)

| Baud | DIP 7 | DIP 8 | `ESCPOS_BAUDRATE` |
|------|-------|-------|-------------------|
| 9600 (default) | OFF | OFF | `9600` |
| 19200 | ON | OFF | `19200` |
| 38400 | OFF | ON | `38400` |
| 115200 | ON | ON | `115200` |

Check your printer manual – mismatch → garbage (e.g. `????`). Test with `FakeSerial` first, then real.

---

## 3. Protocol – ESC/POS Minimal

All thermal printers speak ESC/POS (Epson). Driver implements minimal subset for **fat** etc.; other sequences pass through via `print_raw`.

| Cmd | Bytes (hex) | Method | Notes |
|-----|-------------|--------|-------|
| `ESC @` | `1b 40` | `init_printer()` | Initialize, clear buffer, reset styles |
| `ESC E n` | `1b 45 n` | `set_bold(n)` | `n=1` bold, `0` off (also `ESC G` double-strike) |
| `ESC - n` | `1b 2d n` | `set_underline(n)` | `1` on, `0` off |
| `GS ! n` | `1d 21 n` | `set_size(w,h)` / `set_fat()` | `n = (w-1)<<4 | (h-1)`, `w,h` 1..8. `0x00` normal, `0x01` double height, `0x10` double width, **`0x11` fat** (2× both) |
| `ESC a n` | `1b 61 n` | `set_align(n)` | `0`=left, `1`=center, `2`=right |
| `ESC M n` | `1b 4d n` | `set_font(n)` | `0`=Font A (32col), `1`=Font B (42col) |
| `ESC d n` | `1b 64 n` | `feed(n)` | Feed `n` lines (`n=3` common) |
| `GS V m` | `1d 56 m` | `cut(m)` | `0`=full cut, `1`=partial; needs `0.5s` sleep |
| `DLE EOT n` | `10 04 n` | (future status) | Real-time status |
| `GS ( k` | `1d 28 6b ...` | `print_qr()` placeholder | QR model 2 – printer-specific, driver has placeholder text QR |

**Encoding:** `cp437` (default, Epson USA) or `utf8` via `EscPosPrinter(encoding="cp437")` → `text.encode(encoding, errors="replace")`.

**Timing:** `await asyncio.sleep(0.005)` per `_write` (64 bytes) – thermal needs 1-5 ms; cutter needs `0.5 s`.

**Example bytes:**

```
fat("911") → 1b 40 (init) + 1b 45 01 (bold on) + 1d 21 11 (fat) + 1b 61 01 (center) + "911" + 0a + 1b 45 00 + 1d 21 00 + 1b 61 00
```

Validate via `FakeSerial.get_all().hex()` in tests.

---

## 4. Public API (`escpos.py:1`)

### 4.1 Serial Abstractions

| Class | Purpose |
|-------|---------|
| `FakeSerial` | Tests – `write(data)` appends to `written: list[bytes]`, `get_all()`, `clear()`, `is_open` |
| `FakePigpio` | Tests CI without `pigpiod` – `bb_serial_write(gpio,data)` records `(gpio,data)` |
| `HardwareSerial` | `pyserial` wrapper – `port`/`baudrate`/`timeout`, `open()` via `serial.Serial`, `write(data)`, `close()`, `is_open` |
| `SoftSerial` | `pigpio` bit-bang – `tx_pin`/`rx_pin` BCM + `baudrate`, `open()` via `pigpio.pi().set_mode` + `bb_serial_read_open` or `FakePigpio` fallback, `write(data)` via `wave_add_serial`+`wave_create`+`wave_send_once` or `bb_serial_write`, `is_open` |

### 4.2 `PrintRequest` (`escpos.py:42`)

```python
@dataclass(frozen=True)
class PrintRequest:
    text: str | None = None
    raw_bytes: bytes | None = None
    bold: bool = False
    double_width: bool = False
    double_height: bool = False
    fat: bool = False  # fat = bold+double (overrides bold/double)
    underline: bool = False
    align: str = "left"  # left, center, right
    cut_after: bool = False
    feed_lines: int = 0
```

`EventBus` `PrintRequest` – **main → printer**. If `raw_bytes` set, written directly (plus `cut_after`); else `text` with style flags routed via `print_text`.

### 4.3 `EscPosPrinter` (`escpos.py:89`)

**Constructor – configurable softserial vs hardware, baudrate, pins:**

```python
EscPosPrinter(
    port: str | None = None,          # hardware: "/dev/serial0" or "/dev/ttyUSB0"
    baudrate: int = 9600,             # 9600|19200|38400|57600|115200 – must match DIP
    tx_pin: int | None = None,        # softserial BCM TX (e.g. 27 → pin13) – if set, softserial via pigpio
    rx_pin: int | None = None,        # softserial BCM RX (e.g. 22 → pin15) optional
    timeout: float = 1.0,
    encoding: str = "cp437",
    serial_instance: Any | None = None,      # injection for tests – FakeSerial()
    softserial_instance: Any | None = None,  # injection – FakePigpio
)
# Valid: port+baudrate (hardware) OR tx_pin+baudrate (softserial) OR serial_instance
# Invalid: neither → ValueError
```

- `is_softserial` property, `is_open`, `baudrate`, `tx_pin`/`rx_pin`/`port` attributes.

**Connection (async, idempotent):**

```python
await printer.connect()  # opens HardwareSerial or SoftSerial via run_in_executor, logs, init_printer()
await printer.disconnect()
async with EscPosPrinter(tx_pin=27, baudrate=9600) as p: ...
```

**Low-level:**

```python
await printer._write(data: bytes)  # via soft/hardware, with asyncio.Lock + 0.005s gap
await printer._write_text(text: str)  # encode via self.encoding
await printer.init_printer()  # ESC @
await printer.print_raw(data: bytes)
```

**Primitives:**

```python
await printer.set_bold(True)  # ESC E 1
await printer.set_underline(True)
await printer.set_size(width_mul=2, height_mul=2)  # GS ! 0x11
await printer.set_double_width(True)
await printer.set_double_height(True)
await printer.set_fat(True)  # bold+GS ! 0x11
await printer.set_align("center")  # ESC a 1
await printer.set_font("B")
await printer.feed(3)
await printer.cut(partial=False)  # GS V 0 + 0.5s
```

**High-level convenience (auto-reset styles):**

```python
await printer.print_text("hi", bold=True, double_height=True, align="center", feed_after=1)
await printer.fat("911 ALARM", align="center")  # bold+double @ center
await printer.double_height("hi")
await printer.double_width("hi")
await printer.bold("hi")
await printer.print_qr("https://example.com")  # placeholder text QR
```

- `print_text` with `fat=True` overrides `bold`/`double_*` – does `set_fat(True)` + optional `set_align` + write + `\n` + `set_fat(False)` etc.
- Otherwise sets requested styles, writes, then resets (bold off, size normal, underline off, align left).

**911 alarm helper – main process goal:**

```python
await printer.print_alarm(
    callsign="ALPHA",
    lat=60.1234, lon=24.9876,
    location="Helsinki",
    time=datetime.now(timezone.utc),
    remarks="car crash",
    cot_type="b-a-o-tif",
)
# Paper:
# *** 911 ALARM *** (fat, centered)
# From: ALPHA (bold)
# Loc:  60.12345, 24.98765
#       Helsinki
# Time: 2026-09-07 12:34:56 UTC
# Type: b-a-o-tif
# Remarks: car crash
# --------------------------------
# (feed 2)
```

Signature `print_alarm(callsign, lat?, lon?, location?, time?, remarks?, cot_type?)` – `location` overrides lat/lon string if given.

**EventBus handler:**

```python
await printer.handle_print_request(PrintRequest(text="Hello", fat=True, cut_after=True))
# is_subscribed as: bus.subscribe(PrintRequest, printer.handle_print_request)
```

---

## 5. Configuration

No hard-coded pins/baud – all via constructor or `.env` (see `README_main.md:4`):

| Env | Default | Description |
|-----|---------|-------------|
| `ESCPOS_PORT` / `PRINTER_PORT` | `None` if `TX_PIN` set else `/dev/serial0` | Hardware port – mutually exclusive with `TX_PIN` |
| `ESCPOS_BAUDRATE` | `9600` | 9600|19200|38400|57600|115200 – DIP must match |
| `ESCPOS_TX_PIN` | `None` | BCM TX for softserial (e.g. `27`) – enables pigpio |
| `ESCPOS_RX_PIN` | `None` | BCM RX for softserial (optional) |
| `ESCPOS_ENCODING` | `cp437` | `cp437` or `utf8` |

Injection for tests: `EscPosPrinter(serial_instance=FakeSerial())` or `EscPosPrinter(tx_pin=27, softserial_instance=FakePigpio())`.

---

## 6. Usage Examples (Main Process Can Print)

### 6.1 Direct Async (No Bus)

```python
import asyncio
from tak_bridge_escpos.escpos import EscPosPrinter

async def main():
    # Softserial GPIO when hardware UART busy (chronometer on /dev/serial0)
    async with EscPosPrinter(tx_pin=27, rx_pin=22, baudrate=9600) as p:
        await p.fat("911 ALARM")
        await p.print_text("From: ALPHA", bold=True)
        await p.print_alarm(callsign="ALPHA", lat=60.1, lon=24.8, location="Helsinki")
        await p.cut()
    # Hardware UART
    async with EscPosPrinter(port="/dev/ttyUSB0", baudrate=19200) as p:
        await p.double_height("Double height")
        await p.feed(3)

asyncio.run(main())
```

### 6.2 Via EventBus (Main → Printer Decoupled)

```python
import asyncio
from takpi_common.bus import EventBus
from tak_bridge_escpos.escpos import EscPosPrinter, FakeSerial, PrintRequest

async def main():
    bus = EventBus()
    printer = EscPosPrinter(serial_instance=FakeSerial())  # or tx_pin=27
    await printer.connect()
    bus.subscribe(PrintRequest, printer.handle_print_request)

    # Main can print from anywhere (button handler, CoT handler, timer)
    await bus.publish(PrintRequest(text="Hello", fat=True, align="center", cut_after=True))
    await bus.publish(PrintRequest(text="Double width", double_width=True))
    await bus.publish(PrintRequest(text="911 alarm received from ALPHA at 60.1,24.8 Helsinki", fat=True))
    # Raw ESC/POS bytes also
    await bus.publish(PrintRequest(raw_bytes=b"\x1b@Hello raw\n", cut_after=True))

asyncio.run(main())
```

### 6.3 911 Alarm via CoT (Main → Printer via CotReceived)

```python
import asyncio
from takpi_common.bus import EventBus
from takpi_common.cot_bus import CotBus, CotReceived
from tak_bridge_escpos.escpos import EscPosPrinter

# Assume bus already has HardwareManager and CotBus running via TakPiApp
async def on_cot(evt: CotReceived, bus: EventBus):
    cot = evt.cot
    if hasattr(cot, "is_emergency") and cot.is_emergency():
        callsign = getattr(cot, "callsign", "unknown")
        lat = getattr(cot, "lat", None)
        lon = getattr(cot, "lon", None)
        # Option A: direct printer call (if printer instance in closure)
        # await printer.print_alarm(callsign=str(callsign), lat=lat, lon=lon)
        # Option B: via bus (decoupled, testable)
        from tak_bridge_escpos.escpos import PrintRequest
        # Use fat style via PrintRequest + separate alarm helper
        await bus.publish(PrintRequest(text=f"911 alarm received from {callsign} at {lat},{lon}", fat=True))

# In TakPiApp or __main__.py:
# bus.subscribe(CotReceived, lambda e: on_cot(e, bus))
```

Real `__main__.py:62` does: `CotReceived(b-a-o-tif)` → `_handle_cot_print` → `printer.print_alarm(callsign, lat, lon, location, time, remarks)` + `cut`.

### 6.4 Softserial Config – BCM Pins + Baudrate

```python
# 9600 is safest for softserial bit-bang (pigpio timing)
p = EscPosPrinter(tx_pin=17, baudrate=9600)  # BCM17 pin11 → printer RX
# 19200 also works if pigpio daemon not loaded heavily
p = EscPosPrinter(tx_pin=27, rx_pin=22, baudrate=19200)
# Hardware fallback for bench (USB-TTL dongle)
p = EscPosPrinter(port="/dev/serial/by-id/usb-FTDI_...", baudrate=115200)
```

---

## 7. Testing (No Hardware, No pigpio)

| Test | Coverage |
|------|----------|
| `test_fake_serial_basic` | `FakeSerial` init/`ESC @` |
| `test_softserial_fake_via_injection` | `tx_pin`/`rx_pin` + `FakePigpio` injection, `is_softserial` |
| `test_convenience_fat_double` | `fat` → `ESC E`+`GS ! 0x11`, `double_height`/`double_width`/`bold` |
| `test_print_text_styles_and_align` | `print_text` bold+underline+center, `baudrate` 19200 |
| `test_print_alarm_contains_callsign_and_location` | `print_alarm` contains `911 ALARM`, callsign, lat/lon, location, remarks, `b-a-o-tif` |
| `test_print_request_via_bus` | `EventBus` `PrintRequest` (fat, cut) → `FakeSerial` bytes |
| `test_cot_911_auto_print_via_bus` | `CotReceived` emergency → `print_alarm` |
| `test_configurable_baudrate_and_gpio_pins` | `port`/`baudrate` vs `tx_pin`/`rx_pin`/`baudrate`, `is_softserial`, invalid config |
| `test_header_only` | `ValueError` when no port/tx_pin/instance |

Run:

```bash
poetry install --directory tak-bridge-escpos  # installs takpi-common + takstream path develop + pyserial
poetry run --directory tak-bridge-escpos pytest -v  # 9 tests with FakeSerial/FakePigpio, no Pi/pigpio
poetry run --directory tak-bridge-escpos black --check src tests && poetry run --directory tak-bridge-escpos mypy src && poetry run --directory tak-bridge-escpos pylint src
```

CI uses `FakeSerial`/`FakePigpio` – no Pi, no printer, no `pigpiod` daemon.

---

## 8. Troubleshooting (Driver Layer)

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ValueError: Must provide port or tx_pin` | No config | Set `ESCPOS_PORT` or `ESCPOS_TX_PIN` (BCM) + `ESCPOS_BAUDRATE` |
| Garbage print | Baud mismatch | Match `ESCPOS_BAUDRATE` to printer DIP (try 9600, then 19200, 38400...) – `FakeSerial` test with `baudrate` param helps |
| Nothing on softserial GPIO | `pigpiod` not running or wrong BCM pin (not WiringPi/physical) | `sudo pigpiod; pigs pigs`; use BCM numbers (`27`=pin13, `22`=pin15, `17`=pin11); scope BCM27 pin13 @baudrate with analyzer; `gpioinfo | grep 27` |
| `pigpio daemon not running` warning | `pigpio.pi().connected` false, fallback to `FakePigpio` | `sudo systemctl enable pigpiod; sudo pigpiod`; for CI, inject `FakePigpio` |
| `Permission denied /dev/serial0` | Not in `dialout`/`gpio` | `sudo usermod -a -G dialout,gpio $USER; newgrp dialout`; `ls -l /dev/serial0` |
| `PrintRequest` not printing | No subscriber or wrong `id` | `bus.subscribe(PrintRequest, printer.handle_print_request)` must happen after `await printer.connect()`; check `bus.subscribed(PrintRequest)` |
| Fat not fat | Printer doesn't support `GS !` (rare clone) | Try `double_height` alone; check `encoding` `cp437` vs `utf8` |

---

## 9. Porting & Design Notes

- Softserial via `pigpio` `wave_add_serial` (blocking `wave_create`/`wave_send_once`/`wave_tx_busy` in `run_in_executor`) – same pattern as `mcp23017` `run_in_executor` + `asyncio.Lock` for bus serialization.
- Hardware via `pyserial` `Serial(port, baudrate)` – same as `chronometer` `pyserial` but `timeout` only for RX (printer is TX-only mostly).
- `PrintRequest` frozen dataclass on `EventBus` – same as `LedCommand`/`ButtonEvent` etc., enabling main↔printer decoupling without direct reference.
- 911 helper: main can call `await printer.print_alarm(...)` directly (if it holds printer) **or** publish `PrintRequest(text="911 alarm received from ...", fat=True)` (if decoupled). Both shown in `README_main.md:6`.
- No `python-escpos` dependency – keep async + softserial + EventBus + `pigpio` control; minimal ESC/POS subset is sufficient for 911 paper and can be extended (QR `GS ( k`, barcode `GS k`, image `GS v 0`).

---

## 10. References

- ESC/POS spec: Epson `ESC/POS Application Programming Guide` – `ESC @` (init), `ESC E` (emphasize), `GS !` (char size), `ESC a` (align), `GS V` (cut)
- Pi softserial: `pigpio` `wave_add_serial`/`bb_serial_read_open`, `RPi.GPIO` alternative, `BCM` numbering
- Pi hardware UART: `raspi-config` → Serial, `/dev/serial0` → `ttyAMA0`/`ttyS0`, `common/README_mcp23017.md:2` (I2C BUS) vs this TTL serial
- Bus: `takpi_common/bus.py:1` (`EventBus`), `takpi_common/cot_bus.py:1` (`CotReceived`/`CotSend` via takstream), `takpi_common/app.py:1` (`TakPiApp` example handlers)
- Service: `README_main.md:1` (`__main__.py:1` EventBus+ Cot 911, `ESCPOS_*` env, systemd `pigpiod`)
- Tests: `tak-bridge-escpos/tests/test_escpos.py:1` (9 tests, `FakeSerial`)
- Common: `takpi_common/bus.py:1`, `takpi_common/app.py:1`
- Monorepo: `../../README.md:1` (11 docs), `../../AGENTS.md:1`

---

## 11. Changelog

| Version | Date | Notes |
|---------|------|-------|
| 0.1.0 | 2026-09-07 | Initial async ESC/POS – softserial BCM `tx_pin`/`rx_pin` + hardware `port`, configurable `baudrate` 9600-115200 via `pigpio`/`pyserial`, **fat**/`double_height`/`double_width`/`bold`/`underline`/`align`/`feed`/`cut`, `print_alarm` (911) helper, `PrintRequest` on `EventBus` for main, `FakeSerial`/`FakePigpio` tests, `mypy --strict` clean |

