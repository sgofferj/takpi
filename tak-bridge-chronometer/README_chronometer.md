# README_chronometer – GSA-72 Library Submodule

> **Path:** `takpi/tak-bridge-chronometer/src/tak_bridge_chronometer/chronometer.py:1`  
> **Source port:** `~/Dev/arduIllusion/ArduIllusion/ArduIllusion.h:64` + `ArduIllusion.cpp:32` / `ArduIllusion.cpp:161-201`  
> **Python driver reference:** `~/Dev/arduIllusion/ArduIllusion/driver.py:237`  
> **Hardware:** Flight Illusion GSA-072 Chronometer (Davtron-style) – `GSA72_ID=109` via **Pi GPIO UART**

This document is the thorough reference for the **library submodule** of `tak-bridge-chronometer`. It covers the wire protocol, C++→Python mapping, packet encoding, public API, async design, and testing. For the hourly sync service see `README_main.md:1`; for the bridge overview see `README.md:1` and top-level `../../README_chronometer.md:1`.

---

## 1. Purpose & Scope

The library provides a **pure-Python, async, hardware-faithful** implementation of the Chronometer-specific subset of the ArduIllusion C++ library. Its sole job is to speak the Flight Illusion 6-byte serial protocol to the GSA-72 clock **over the Raspberry Pi's GPIO UART (`/dev/serial0`)**.

**Goals**
- Bit-exact replica of `FIGaugeSet::sendCommand` (C++ `ArduIllusion.cpp:32`)
- One-to-one mapping for every `gsa72_*` C++ method (C++ `ArduIllusion.cpp:161`)
- Async `pyserial` on `/dev/serial0` (`GPIO14`/`GPIO15`) with the mandatory 2 ms/byte gap
- Testability without hardware via pure `encode_packet()` and `MockSerial`
- No TAK dependency – the gauge is a local display, not a COT producer

**Non-goals**
- No TAK Server, no `pytak`, no mission logic (that belongs to `common/` if needed later)
- No GUI, no clock drift compensation beyond hourly resync (see service doc)

---

## 2. Hardware Reference

| Item | Value |
|------|-------|
| Product | [GSA-072 Chronometer (Davtron)](https://www.flightillusion.com/military-vintage/chrono-gauges-mil/gsa-072-chronometerclock-davtron-style/) |
| Vendor | Flight Illusion (Reuschling) |
| Display | Local time (hour), UTC (H:M:S), Flight time, Volts, Temp °C/°F |
| Interface | **TTL UART 3.3V via Raspberry Pi GPIO UART** – `GPIO14` (TXD, header pin 8) / `GPIO15` (RXD, header pin 10) at 38400 baud, exposed as `/dev/serial0` (`→ ttyAMA0` on Pi 4/5 or `ttyS0` on Pi Zero/3) |
| Baudrate | **38400** 8N1 (fixed, per manual `Fight-Illusion-manual-2016.pdf:22`) |
| Gauge ID | `109` (`GSA72_ID` `ArduIllusion.h:47`) |
| Commands | `SETLOCAL=4`, `SETUTC=5`, `SETFLT=6`, `SETVOLT=9`, `SETTEMPC=11`, `SETTEMPF=12`, plus generic `RESET=1`/`SETADR=2`/`QUERY=7`/`SETLIGHT=8` |
| Pi UART | `UART0` (`ttyAMA0` or `ttyS0`), alias `serial0`; enable via `raspi-config` + `dtoverlay=disable-bt` + disable `serial-getty@serial0` |

### 2.1 Wiring – Pi GPIO Direct (no USB dongle)

> **This bridge uses the Pi's hardware UART on the GPIO header, not a USB-FTDI adapter.** USB `ttyUSB0`/`by-id` paths are for bench testing only; production is GPIO.

```
FI Gauge 10-pin (rear)                 Raspberry Pi 40-pin Header (BCM)
----------------------                 --------------------------------
Pin 1  +5V        ┐                    *Not from Pi – gauge needs external +5V/+12V PSU*
Pin 2  +12V       │ Power (from gauge PSU, NOT Pi header 5V)
Pin 3  +5V        ┘                    Keep PSU GND common with Pi GND
Pin 4  +5V                            

Pin 5  GND ─────────┐                  Pin 6   GND ┐
Pin 6  GND ─────────┤ Common ground    Pin 9   GND ├─ Use any GND (6,9,14,20,25,30,34,39)
Pin 7  GND ─────────┘                  Pin 14  GND ┘
Pin 9  GND ──────────────────────────── Pin 6/14 GND (star ground)

Pin 8  TxD (TTL 3.3V) ───────────────── Pin 10  GPIO15 RXD (Pi UART RX ← Gauge Tx)
                                        (optional – gauge rarely replies; can leave NC)

Pin 10 RxD (TTL 3.3V) ───────────────── Pin 8   GPIO14 TXD (Pi UART TX → Gauge Rx)
```

**Level notes:** Pi GPIO is **3.3V TTL**, not 5V-tolerant. FI gauge manual states TTL levels; most GSA-72 units are 3.3V-compatible at 38400 baud. Do **not** drive Pi `RXD` with 5V – if gauge `TxD` measures 5V under load, add a resistive divider (e.g., 1k/2k) or a 3.3V level shifter. Keep wires <30 cm, twisted with GND for EMI.

**Enable UART (Pi OS Bookworm):**

```bash
# raspi-config → Interface Options → Serial Port
#   Login shell over serial: No
#   Serial port hardware: Yes
# or edit /boot/firmware/config.txt:
#   enable_uart=1
#   dtoverlay=disable-bt   # on Pi 3/4 if BT steals AMA0

# Remove console serial from cmdline
sudo sed -i 's/console=serial0,[0-9]* //;s/console=ttyAMA0,[0-9]* //' /boot/firmware/cmdline.txt

sudo systemctl disable --now serial-getty@serial0.service
sudo systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
sudo reboot

ls -l /dev/serial0  # → ../ttyAMA0 or ../ttyS0
dmesg | grep -i uart  # fe201000.serial: ttyAMA0 at MMIO ...
```

Use `CHRONO_PORT=/dev/serial0` in `.env` – the canonical symlink that always points to the correct `ttyAMA0`/`ttyS0`. Add service user to `dialout`: `sudo usermod -a -G dialout pi`.

### 2.2 Power

Gauge requires **+12 V and +5 V rails** (Pins 1-4) plus common ground. **Do not power the gauge from the Pi's 5V header pins** (insufficient current, noisy). Use the gauge's external PSU; only share GND (at least one of Pins 5-7/9 to Pi GND). TTL dongle isolation note from manual is moot for GPIO – direct connection is intended.

---

## 3. Wire Protocol

All ArduIllusion gauges share the same 6-byte frame. The Chrono uses only commands `4,5,6,9,11,12` with `id=109` on `GPIO14` TXD.

### 3.1 Frame Layout (`ArduIllusion.cpp:32 sendCommand`)

```c
byte buffer[6];
buffer[0]=0x00;            // header
buffer[1]=id;              // gauge ID, e.g. 109
buffer[5]=0xff;            // terminator
long1 = abs(value);
int2  = long1 >> 8;
buffer[3] = (long1 & 0xff) | 0x01;   // low byte + flag bit0
buffer[4] = (int2  & 0xff) | 0x02;   // high byte + flag bit1
bytethree = cmd << 4;                // high nibble = command
bytethree |= (long1 & 0x01);         // bit0 = LSB of long1
bytethree |= (int2  & 0x02);         // bit1 = bit1 of int2
if (value < 0) bytethree |= 0x0C;    // bits3:2 = 11 for negative
else           bytethree |= 0x08;    // bits3:2 = 10 for positive
buffer[2]=bytethree;
for cnt=0..5: gaugePort.write(buffer[cnt]); delay(2);
```

**Diagram**

```
Byte:  0      1      2                  3               4               5
     [0x00] [ID] [CCCC BBNN] [DDDD DDD1] [DDDD DD1X] [0xFF]
            │     ││││ │││    │       │    │      │     │
            │     ││││ │││    └───────┘    └──────┘     └── terminator
            │     ││││ ││└─ bit1 of int2         low 8 bits of abs(value) |0x01
            │     ││││ │└── LSB of long1         high 8 bits of abs(value)>>8 |0x02
            │     ││││ └─ sign: 0x08=+, 0x0C=-   (bits3:2)
            │     │││└─ bit1 of int2 (duplicate)
            │     ││└── LSB of long1 (duplicate)
            │     │└── command in high nibble
            │     └─ GPIO14 TXD (pin 8) with 2 ms delay per byte on /dev/serial0
            └─ 109 for GSA-72
```

### 3.2 Worked Examples

**Example A – `gsa72_set_local(14)` → `value=14`, `cmd=4`, `id=109`**

```
long1=14 (0x0E), int2=0
byte_three = (0x0E|0x01)=0x0F
byte_four  = (0x00|0x02)=0x02
byte_two   = (4<<4)=0x40 | (14&0x01)=0x00 | (0&0x02)=0x00 |0x08 =0x48
packet = 00 6D 48 0F 02 FF   (0x6D=109)  → 6 bytes on GPIO14 pin 8 @38400
```

**Example B – `gsa72_set_utc_hms(12,34,56)` → `value=45296 (0xB0F0)`**

```
long1=45296, int2=176 (0xB0)
byte_three = (0xF0|0x01)=0xF1
byte_four  = (0xB0|0x02)=0xB2
byte_two   = 0x50 | (0xF0&0x01)=0x00 | (0xB0&0x02)=0x00 |0x08 =0x58
packet = 00 6D 58 F1 B2 FF
```

**Example C – negative wrap `gsa72_set_utc(70000)` → C++ `65535-70000=-4465`**

```
value=-4465 → long1=4465 (0x1171), int2=17 (0x11)
byte_two flag 0x0C, packet = 00 6D 5D 71 12 FF  (approx, see test)
```

Validate via `encode_packet(109,5,-4465)` in `tests/test_chronometer.py:18`.

### 3.3 Timing

`delay(2)` per byte = 12 ms per frame + 50 ms inter-command gap added in `__main__.py:83` to avoid gauge overrun on the Pi's much faster UART (vs Arduino 16 MHz).

---

## 4. C++ → Python Mapping

| C++ Header `ArduIllusion.h:64` | C++ Impl `ArduIllusion.cpp:161` | Python `chronometer.py` | Notes |
|---|---|---|---|
| `GSA72_ID 109` | – | `GSA72_ID = 109` | – |
| `GSA72_CMD_SETLOCAL 4` | – | `GSA72_CMD_SETLOCAL = 4` | – |
| `GSA72_CMD_SETUTC 5` | – | `GSA72_CMD_SETUTC = 5` | – |
| `GSA72_CMD_SETFLT 6` | – | `GSA72_CMD_SETFLT = 6` | – |
| `GSA72_CMD_SETVOLT 9` | – | `GSA72_CMD_SETVOLT = 9` | – |
| `GSA72_CMD_SETTEMPC 11` | – | `GSA72_CMD_SETTEMPC = 11` | – |
| `GSA72_CMD_SETTEMPF 12` | – | `GSA72_CMD_SETTEMPF = 12` | – |
| `void gsa72_setUTC(long sec)` `ArduIllusion.cpp:163` | `if(sec>65535) sec=65535-sec; sendCommand(109,5,sec);` | `async def gsa72_set_utc(self, seconds: int)` `chronometer.py:211` | Wrap preserved |
| `void gsa72_setUTC(byte h,m,s)` `cpp:168` | `value=h*3600+m*60+s; if>65535 wrap; sendCommand` | `gsa72_set_utc_hms(self, h,m,s)` `chronometer.py:222` | Helper |
| `void gsa72_setFLT(long sec)` `cpp:174` | same wrap | `gsa72_set_flt` `chronometer.py:234` | – |
| `void gsa72_setFLT(byte h,m,s)` `cpp:179` | same wrap | `gsa72_set_flt_hms` `chronometer.py:243` | – |
| `void gsa72_setLocal(byte h)` `cpp:185` | `sendCommand(109,4,h);` | `gsa72_set_local` `chronometer.py:250` | `0-23` |
| `void gsa72_setTempC(int dc)` `cpp:189` | `tempc=(dc+50)/100; tempf=9/5*(tempc+32); send both` | `gsa72_set_temp_c` `chronometer.py:258` | **Bug fix** (see §5) |
| `void gsa72_setVolt(long mV)` `cpp:196` | `volts=mV/1000; dV=(mV-volts*1000)/100; value=(volts<<8)+dV;` | `gsa72_set_volt` `chronometer.py:279` | – |
| `void sendCommand(id,cmd,val)` `cpp:32` | 6-byte encoder + `delay(2)` | `encode_packet()` `chronometer.py:52` + `ChronometerClient._send_command` `chronometer.py:173` | Extracted for tests; `_send_command` drives `/dev/serial0` |

General FI helpers also exposed (useful for reset/light): `init_gauge` (`CMD_RESET=1`), `query_gauge` (`CMD_QUERY=7`), `set_light` (`CMD_SETLIGHT=8`), `set_address` (`CMD_SETADR=2`).

---

## 5. Notable Deviation – Temperature Bug

C++ `ArduIllusion.cpp:191`:

```c
byte tempf = 9/5*(tempc+32); // 9/5 integer division = 1 → tempf = tempc+32 (bug)
```

Python `chronometer.py:274` intentionally corrects to the *intended* formula:

```python
tempf = int((1.8 * tempc) + 32)
```

`tempc` still uses faithful `(decicelsius+50)//100`. Both commands are still sent (`SETTEMPC` then `SETTEMPF`) so gauge receives correct pair. Comment in code explains the deviation.

---

## 6. Public API Reference

### 6.1 `encode_packet(gauge_id: int, cmd: int, value: int) -> bytes` `chronometer.py:52`

Pure function, no I/O. Implements `sendCommand` bit math exactly (see §3.1). Used by `ChronometerClient` and directly in tests/CI – no GPIO needed.

```python
from tak_bridge_chronometer.chronometer import encode_packet, GSA72_ID, GSA72_CMD_SETUTC

pkt = encode_packet(GSA72_ID, GSA72_CMD_SETUTC, 45296)
assert pkt == bytes.fromhex("00 6D 58 F1 B2 FF")
```

### 6.2 `class ChronometerClient` `chronometer.py:98`

Async context manager; holds `pyserial` handle for ` /dev/serial0`, `asyncio.Lock`, and `port`/`baudrate`/`timeout`.

**Constructor**

```python
ChronometerClient(port: str = "/dev/serial0", baudrate: int = 38400, timeout: float = 1.0)
# Recommended: ChronometerClient(port="/dev/serial0")  # GPIO14/15
# Bench-test fallback: port="/dev/ttyUSB0" also works (warns, see __main__.py:133)
```

`port` defaults to GPIO alias ` /dev/serial0` (canonical → `ttyAMA0` or `ttyS0`). `timeout` is `pyserial` read timeout (unused for writes).

**Connection**

```python
await client.connect()      # idempotent, 2 s settle, uses run_in_executor, opens /dev/serial0
await client.disconnect()   # closes handle
async with ChronometerClient(port="/dev/serial0") as c: ...  # preferred
```

`connect()` creates `asyncio.Lock` lazily if needed, logs to `tak_bridge_chronometer.chronometer`.

**Low-level**

```python
await client._send_command(gauge_id, cmd, value)  # raises ConnectionError if not connected
# Writes bytes one-by-one via executor + await asyncio.sleep(0.002) on GPIO14 TXD
```

**Generic FI**

```python
await client.init_gauge(gauge_id=GSA72_ID)  # RESET
await client.query_gauge(gauge_id=GSA72_ID)
await client.set_light(gauge_id, light)     # DL000000
await client.set_address(gauge_id, address)
```

**GSA-72**

```python
await client.gsa72_set_utc(seconds: int)
await client.gsa72_set_utc_hms(hours, minutes, seconds)
await client.gsa72_set_flt(seconds)
await client.gsa72_set_flt_hms(hours, minutes, seconds)
await client.gsa72_set_local(hours: int)          # 0-23
await client.gsa72_set_temp_c(decicelsius: int)   # e.g. 2150 = 21.5°C → 21°C/69°F
await client.gsa72_set_volt(millivolts: int)      # e.g. 28500 = 28.5V
```

All `gsa72_*` preserve the `>65535 → 65535-value` wrap and drive GPIO14.

**Alias**

```python
from tak_bridge_chronometer.chronometer import FlightIllusionChronometerClient
# identical to ChronometerClient (backwards compat with driver.py name)
```

---

## 7. Async Design & Thread Safety

- Each `write` runs in `loop.run_in_executor(None, serial.write)` to avoid blocking the event loop (pyserial is sync, even on GPIO UART).
- `asyncio.Lock` serializes frames: concurrent `gsa72_set_*` calls do not interleave bytes on GPIO14.
- `asyncio.sleep(0.002)` per byte + `0.05` s inter-command gap (service layer) ensures gauge MCU can keep up (observed necessary in Arduino `delay(2)`).
- `connect()`/`disconnect()` are idempotent; service layer (`__main__.py:114`) handles reconnect with exponential backoff (`1 → 60 s`).

**Example concurrent safety**

```python
await asyncio.gather(
    client.gsa72_set_local(14),
    client.gsa72_set_utc_hms(12, 34, 56),
)  # lock ensures two full 6-byte frames on GPIO14, not interleaved
```

---

## 8. Usage Examples

### 8.1 Minimal – Set Time Once via GPIO

```python
import asyncio
from tak_bridge_chronometer.chronometer import ChronometerClient

async def main():
    async with ChronometerClient(port="/dev/serial0") as c:  # GPIO14 TXD pin 8
        await c.gsa72_set_local(14)
        await c.gsa72_set_utc_hms(12, 34, 56)

asyncio.run(main())
```

### 8.2 Full Gauge Exercise (volts + temp + flight timer) via GPIO

```python
async with ChronometerClient(port="/dev/serial0") as c:
    await c.init_gauge()                 # reset via GPIO
    await c.gsa72_set_local(9)           # local 09:00
    await c.gsa72_set_utc_hms(7, 30, 0)  # UTC 07:30:00
    await c.gsa72_set_flt_hms(2, 15, 0)  # FLT 02:15:00
    await c.gsa72_set_volt(24200)        # 24.2 V → gauge shows 24.2
    await c.gsa72_set_temp_c(2150)       # 21.5°C → 21°C + 69°F displayed
    await c.set_light(109, 0b11000000)   # lights per gauge manual
```

### 8.3 Direct Packet Inspection (no hardware, no GPIO)

```python
from tak_bridge_chronometer.chronometer import encode_packet, GSA72_ID, GSA72_CMD_SETLOCAL

pkt = encode_packet(GSA72_ID, GSA72_CMD_SETLOCAL, 14)
print(pkt.hex(" "))  # 00 6d 48 0f 02 ff  → would be sent on GPIO14 TXD
```

---

## 9. Testing

### 9.1 Unit Tests (no hardware, no GPIO) `tests/test_chronometer.py:1`

| Test | Coverage |
|------|----------|
| `test_encode_packet_basic` | header/id/terminator, positive flag |
| `test_encode_packet_negative` | negative flag `0x0C` |
| `test_encode_packet_bit_embedding` | LSB/bit1 embedding into `byte_two` |
| `test_encode_packet_data_bytes` | `0x01`/`0x02` forcing |
| `test_gsa72_set_utc_wraps_over_65535` | `>65535` wrap + negative packet |
| `test_gsa72_set_local_and_volt_encoding` | `encode_packet` vs mocked `write` on GPIO |
| `test_sync_once_calls_both_setters` | service calls both setters with valid ranges |

Run (no Pi GPIO needed):

```bash
poetry install --directory tak-bridge-chronometer
poetry run --directory tak-bridge-chronometer pytest -v
poetry run --directory tak-bridge-chronometer black --check src tests
poetry run --directory tak-bridge-chronometer mypy src   # strict, ignore_missing_imports
poetry run --directory tak-bridge-chronometer pylint src # 10.00
```

### 9.2 Mock Serial Pattern

Tests patch `asyncio.get_running_loop().run_in_executor` to call `write` synchronously and `asyncio.sleep` to `AsyncMock` to avoid 2 ms delays. Use `MagicMock` with `is_open=True` and capture `write` calls – no `/dev/serial0` required. GPIO is fully mocked.

### 9.3 Hardware-in-the-Loop (GPIO)

```bash
# Enable UART, wire Pi pin 8 → gauge Pin 10, GND common
CHRONO_PORT=/dev/serial0 poetry run --directory tak-bridge-chronometer python -m tak_bridge_chronometer
# Check: scope GPIO14 pin 8 @38400 – should see 00 6D ... FF frames
```

Inspect gauge: local hour should match Pi `date`, UTC should match `date -u`. Logic analyzer on GPIO14 confirms timing 2 ms/byte.

---

## 10. Porting Notes

- Extracted `encode_packet` as pure function for testability (not present in `driver.py`).
- Kept `>65535` wrap verbatim (even though it yields negative values; matches gauge firmware expectation).
- Fixed temp conversion (see §5) but documented deviation.
- Added `asyncio.Lock` and executor pattern absent in Arduino (which is single-threaded, but Pi UART also needs serialization).
- GPIO path: Arduino `gaugePort Serial3` → Pi `/dev/serial0` (`/dev/ttyAMA0`/`ttyS0`) – same framing, same baud, same 2 ms gap, but Pi requires `enable_uart` + `dialout` + `disable-bt` handling.
- Type hints added for `mypy --strict` (`__init__.py:1` re-exports).

---

## 11. Troubleshooting (Library Layer)

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ConnectionError: Not connected` | Forgot `await client.connect()` or used closed client | Use `async with` |
| `SerialException: could not open port /dev/serial0` | UART not enabled or permission | `raspi-config` enable UART, remove `console=serial0` from `cmdline.txt`, `sudo usermod -a -G dialout $USER`; verify `ls -l /dev/serial0` |
| `SerialException: [Errno 13] Permission denied` | Not in `dialout` | `sudo usermod -a -G dialout $USER` + relogin; `ls -l /dev/serial0` should be `crw-rw---- dialout` |
| Gauge ignores commands on GPIO | Wrong baud (must 38400) or missing 2 ms gap or wired to wrong GPIO | Check `baudrate=38400`, ensure not bypassing `_send_command`, verify Pi pin 8 (GPIO14) → gauge Pin 10 RxD, GND common, scope GPIO14 @38400 |
| `No such file /dev/serial0` | UART disabled / BT overlay missing | `enable_uart=1`, `dtoverlay=disable-bt` on Pi 3/4, `systemctl disable serial-getty@serial0`, reboot; `dmesg \| grep uart` |
| Packets look wrong in logic analyzer on GPIO14 | Flag math error | Compare against `encode_packet` examples §3.2 (scope GPIO14 TXD) |
| Using `/dev/ttyUSB0` warning | Bench-testing with USB adapter | Set `CHRONO_PORT=/dev/serial0` for production GPIO; USB works but service warns (see `__main__.py:133`) |

---

## 12. References

- C++ library: `~/Dev/arduIllusion/ArduIllusion/ArduIllusion.h:44` (`GSA72_ID`) + `ArduIllusion.h:64` (prototypes) + `ArduIllusion.cpp:32` (`sendCommand`) + `ArduIllusion.cpp:161` (Chrono)
- Python driver: `~/Dev/arduIllusion/ArduIllusion/driver.py:53` (`GSA72_CMD_*`) + `driver.py:237` (`gsa72_*` methods)
- Manual: `~/Dev/arduIllusion/Fight-Illusion-manual-2016.pdf:22` (baud, wiring) + gauge pinout `README.md:17` – **add Pi GPIO mapping `pin 8→GPIO14`, `pin10→GPIO15`**
- Pi UART: `raspi-config`, `/boot/firmware/config.txt` (`enable_uart`, `dtoverlay=disable-bt`), `cmdline.txt` (remove console), `serial-getty@serial0`
- Gauge product: https://www.flightillusion.com/military-vintage/chrono-gauges-mil/gsa-072-chronometerclock-davtron-style/
- Tests: `tak-bridge-chronometer/tests/test_chronometer.py:1`
- Service counterpart: `README_main.md:1` (hourly sync via GPIO)

---

## 13. Changelog

| Version | Date | Notes |
|---------|------|-------|
| 0.1.0 | 2026-09-07 | Initial port – `encode_packet` + `ChronometerClient` on **GPIO UART `/dev/serial0`**, 7 gsa72 methods, `mypy --strict` clean |

