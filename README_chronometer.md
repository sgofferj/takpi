# README_chronometer – tak-bridge-chronometer Bridge

> **Monorepo:** `takpi/` (Raspberry Pi TAK Hardware Bridge Collection)  
> **Bridge path:** `tak-bridge-chronometer/`  
> **Status:** ✅ Implemented – library + hourly `local`/`UTC` sync  
> **Hardware:** Flight Illusion GSA-072 Chronometer (Davtron) – `serial:/dev/serial0 @38400` (GPIO UART, TTL 3.3V)  
> **Docs:** Bridge `tak-bridge-chronometer/README.md:1`, Library `tak-bridge-chronometer/README_chronometer.md:1`, Service `tak-bridge-chronometer/README_main.md:1`

This is the **top-level thorough documentation** for the `tak-bridge-chronometer` submodule of `takpi`. It aggregates the library and service docs into one entry point for the monorepo index (`README.md:1` and `AGENTS.md:84`).

---

## 1. At a Glance

| Attribute | Value |
|-----------|-------|
| Bridge name | `tak-bridge-chronometer` (PyPI `tak-bridge-chronometer`) |
| Python package | `tak_bridge_chronometer` (`src/tak_bridge_chronometer/`) |
| Entry | `poetry run start` → `tak_bridge_chronometer.__main__:run` |
| Library | `src/tak_bridge_chronometer/chronometer.py:1` – port of `ArduIllusion.cpp:161` |
| Service | `src/tak_bridge_chronometer/__main__.py:1` – `sync_once` every `3600` s |
| Interface | TTL UART 38400 8N1 via **Pi GPIO UART** (`/dev/serial0` → `ttyAMA0`/`ttyS0`), 6-byte FI frame with 2 ms/byte gap (`ArduIllusion.cpp:32`) |
| Gauge ID | `109` (`GSA72_ID`), commands `SETLOCAL=4`/`SETUTC=5`/`SETFLT=6`/`SETVOLT=9`/`SETTEMPC=11`/`SETTEMPF=12` |
| Source C++ | `~/Dev/arduIllusion/ArduIllusion/ArduIllusion.h:64` + `ArduIllusion.cpp:161-201` |
| Systemd | `tak-bridge-chronometer/systemd/tak-bridge-chronometer.service:1` |
| Health | `/tmp/tak-chronometer-healthy` (absent = healthy) |

**What it does:** Every hour the Pi's **local hour** and **UTC H:M:S** are pushed to the GSA-72 clock so the analog gauge stays aligned with NTP without manual setting. Voltage/temp/flight timer APIs are available via the library but not polled by default.

---

## 2. Why This Bridge Exists

The GSA-72 is a popular Davtron-style GA clock for sim pits and UAV ground stations (see `~/Dev/arduIllusion/README.md:12`). Unlike modern avionics it lacks persistent RTC sync when paired with a Pi that may lose power or lack RTC. The bridge:

- Eliminates manual clock setting after every Pi reboot
- Keeps local hour correct across DST transitions (`Europe/Helsinki` etc.)
- Keeps UTC authoritative via Pi's `systemd-timesyncd`/`chrony` (same source TAK uses for CoT timestamps)
- Fits the `takpi` pattern: one Pi, many independent hardware bridges, each with own `pyproject.toml`/systemd, no cross-bridge coupling except `takpi_common`

It is **not** a TAK COT feeder (no `pytak`), but it follows the same `AGENTS.md:14` runtime pattern (asyncio, health file, `dialout`, GPIO UART).

---

## 3. Hardware & Wiring

### 3.1 Gauge

- Product: [GSA-072 Chronometer/Clock – Davtron-style](https://www.flightillusion.com/military-vintage/chrono-gauges-mil/gsa-072-chronometerclock-davtron-style/) (Flight Illusion)
- Displays: Local (hour), UTC, Flight time, Volts, Outside air temp
- Connector: 10-pin FI harness (see `arduIllusion/README.md:17`):

```
FI Gauge 10-pin (rear)            Raspberry Pi 40-pin GPIO Header
Pin 1  +5V        ┐
Pin 2  +12V       │ Power (from gauge PSU, NOT Pi 5V)
Pin 3  +5V        ┘
Pin 4  +5V
Pin 5  GND ─────────┐               Pin 6  GND  ┐
Pin 6  GND ─────────┤ Common ground  Pin 9  GND  ├─ GND (any of 6,9,14,20,25,30,34,39)
Pin 7  GND ─────────┘               Pin 14 GND ┘
Pin 8  TxD (TTL 3.3V) ─────────────── Pin 10 GPIO15 RXD (Pi RX ← Gauge Tx, optional – gauge rarely replies)
Pin 9  GND ────────────────────────── Pin 6  GND (or other GND)
Pin 10 RxD (TTL 3.3V) ─────────────── Pin 8  GPIO14 TXD (Pi TX → Gauge Rx)
```

- **Pi side:** Direct GPIO UART, no USB dongle. Pi `GPIO14` (header pin 8, `UART0_TXD`) drives gauge `RxD`; `GPIO15` (pin 10, `UART0_RXD`) optionally reads gauge `TxD`. Both at **3.3V TTL** (gauge is 5V-tolerant per manual but Pi is NOT 5V-tolerant – do **not** apply 5V to Pi pins). Level shifter only needed if gauge Tx is 5V push-pull under load – measure; most FI gauges are 3.3V-compatible at 38400 baud.

### 3.2 Enable GPIO UART on Pi

```bash
# 1. raspi-config → Interface Options → Serial Port:
#    - Login shell over serial: No
#    - Serial port hardware: Yes
# Or manually in /boot/firmware/config.txt:
#    dtoverlay=disable-bt  # on Pi 3/4 if BT claims AMA0, moves BT to miniuart
#    enable_uart=1

# 2. Remove console from cmdline
sudo sed -i 's/console=serial0,[0-9]* //;s/console=ttyAMA0,[0-9]* //' /boot/firmware/cmdline.txt
# Verify cmdline.txt no longer contains console=serial0

# 3. Disable getty on serial0
sudo systemctl disable --now serial-getty@serial0.service
sudo systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true

# 4. Reboot, then verify
ls -l /dev/serial0  # → /dev/ttyAMA0 (Pi 4/5) or /dev/ttyS0 (Pi Zero/3 with BT)
ls -l /dev/ttyAMA0 /dev/ttyS0 2>&1
dmesg | grep -i uart
# Should show: [    0.8] fe201000.serial: ttyAMA0 at MMIO ...
```

`/dev/serial0` is the canonical alias (systemd `hwdb` symlink) – use it in `.env` (`CHRONO_PORT=/dev/serial0`). Do **not** use `/dev/ttyUSB0` or `/dev/serial/by-id/*` – those are for USB adapters, not GPIO.

### 3.3 Permissions & Electrical

```bash
sudo usermod -a -G dialout $USER   # and for service User=pi
# relogin or newgrp dialout
groups  # should include dialout
ls -l /dev/serial0  # crw-rw---- 1 root dialout
```

- Wire gauge GND to Pi GND (mandatory, at least one GND pin 6).
- Power gauge +12V/+5V from its own PSU, not from Pi 5V pin (insufficient current, noise).
- Keep TTL wires short (<30 cm), twisted with GND if possible.
- Gauge draws ~150 mA @12V; Pi GPIO draws <2 mA – no buffer needed at 38400 baud.

Failure mode: `SerialException: Permission denied /dev/serial0` – see permissions above; `No such file /dev/serial0` – UART not enabled (see §3.2).

---

## 4. Protocol Summary

Full deep dive: `tak-bridge-chronometer/README_chronometer.md:3`.

**Frame:** `[0x00, ID, CMD_FLAGS, DATA_LOW|0x01, DATA_HIGH|0x02, 0xFF]` – 6 bytes, 2 ms per byte (`ArduIllusion.cpp:32` `sendCommand`).

**Flags in `CMD_FLAGS` (byte 2) high nibble = command:**

```
byte2 = (cmd<<4) | (long1&0x01) | (int2&0x02) | (0x08 for + / 0x0C for -)
```

**Data bytes:**

```
byte3 = (long1 & 0xff) | 0x01
byte4 = (int2  & 0xff) | 0x02   where long1=abs(value), int2=long1>>8
```

**Commands used:**

| Cmd | C++ | Python | Value meaning |
|-----|-----|--------|---------------|
| 4 | `GSA72_CMD_SETLOCAL` `ArduIllusion.h:65` | `gsa72_set_local(h)` | `0-23` local hour |
| 5 | `GSA72_CMD_SETUTC` `h:66` | `gsa72_set_utc` / `gsa72_set_utc_hms` | `h*3600+m*60+s` seconds since midnight, wrap `>65535` as `65535-value` |
| 6 | `GSA72_CMD_SETFLT` `h:67` | `gsa72_set_flt` | flight timer seconds |
| 9 | `GSA72_CMD_SETVOLT` `h:68` | `gsa72_set_volt(mV)` | `(volts<<8)|decivolts` |
| 11 | `GSA72_CMD_SETTEMPC` `h:69` | `gsa72_set_temp_c(dc)` | `(dc+50)//100` °C |
| 12 | `GSA72_CMD_SETTEMPF` `h:70` | (sent with above) | `1.8*C+32` (C++ bugfix) |

**Examples (hex, via GPIO `serial0`):**

```
set_local(14)            → 00 6d 48 0f 02 ff  (6 bytes on GPIO14 TXD pin 8, 38400 baud)
set_utc_hms(12,34,56)     → 00 6d 58 f1 b2 ff  (45296 = 0xb0f0)
set_volt(28500)           → 00 6d 9? ?? ?? ff (7173 = 0x1c05)
```

Validate with `tak_bridge_chronometer.chronometer.encode_packet` (pure, testable) – no GPIO needed.

---

## 5. Submodules

The bridge consists of two Python submodules, each with its own thorough doc:

| Submodule | File | Doc | Purpose |
|-----------|------|-----|---------|
| `chronometer` (library) | `tak-bridge-chronometer/src/tak_bridge_chronometer/chronometer.py:1` | `tak-bridge-chronometer/README_chronometer.md:1` | Port of `ArduIllusion.cpp:161` GSA-72 + `encode_packet` replica of `sendCommand` (GPIO UART) |
| `__main__` (service) | `tak-bridge-chronometer/src/tak_bridge_chronometer/__main__.py:1` | `tak-bridge-chronometer/README_main.md:1` | `sync_once()` `__main__.py:52` → `gsa72_set_local` + `gsa72_set_utc_hms` every `UPDATE_INTERVAL` with backoff/health |

Both are `black`/`mypy --strict`/`pylint` clean and `pytest` covered (`tests/test_chronometer.py:1` – 7 tests, mocks; GPIO mocked).

---

## 6. Quick Start

### 6.1 Install (on Pi or dev laptop with venv)

```bash
cd ~/Dev/TAK/takpi
poetry install --directory tak-bridge-chronometer
# dev
poetry install --directory tak-bridge-chronometer --with dev
```

Poetry creates `tak-bridge-chronometer/.venv` (isolated, per-user venv request).

### 6.2 Configure `.env`

Create `tak-bridge-chronometer/.env` (ignored):

```
CHRONO_PORT=/dev/serial0
CHRONO_BAUDRATE=38400
UPDATE_INTERVAL=3600
HEALTH_FILE=/tmp/tak-chronometer-healthy
```

For Pi 5 with `ttyAMA0` explicitly: `CHRONO_PORT=/dev/ttyAMA0` also works, but `serial0` is preferred alias.  
For dev without hardware (CI): `CHRONO_PORT=/dev/null` (tests mock serial).

### 6.3 Run Foreground (validate wiring)

```bash
poetry run --directory tak-bridge-chronometer start
# logs: "Syncing Chronometer – local 14:00 (local EEST) UTC 11:00:04"
# Ctrl-C → graceful disconnect
```

Check gauge: local hour should jump to Pi local hour, UTC to `date -u`. Verify wiring: Pi pin 8 (TX) → gauge Pin 10 (Rx), GND continuity.

For quick validation every 10 s:

```bash
UPDATE_INTERVAL=10 poetry run --directory tak-bridge-chronometer start
```

### 6.4 Tests (no hardware)

```bash
poetry run --directory tak-bridge-chronometer pytest -v
poetry run --directory tak-bridge-chronometer black --check src tests
poetry run --directory tak-bridge-chronometer mypy src
poetry run --directory tak-bridge-chronometer pylint src
# expected: 7 passed, black clean, mypy "Success: no issues", pylint 10.00
```

### 6.5 Systemd (production)

```bash
sudo cp tak-bridge-chronometer/systemd/tak-bridge-chronometer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tak-bridge-chronometer
systemctl status tak-bridge-chronometer
journalctl -u tak-bridge-chronometer -f
cat /tmp/tak-chronometer-healthy || echo "healthy"  # no file = healthy
```

See `tak-bridge-chronometer/README_main.md:7` for full systemd table, troubleshooting, and monitoring. Service uses `User=pi Group=dialout` and `WorkingDirectory` with GPIO UART – no USB `by-id` path.

---

## 7. Library Usage (Direct, Without Service)

```python
import asyncio
from tak_bridge_chronometer.chronometer import ChronometerClient

async def demo():
    async with ChronometerClient(port="/dev/serial0") as c:
        await c.gsa72_set_local(9)            # 09:00 local
        await c.gsa72_set_utc_hms(7, 30, 0)   # 07:30:00 UTC
        await c.gsa72_set_flt_hms(1, 20, 0)   # 01:20:00 FLT
        await c.gsa72_set_volt(24200)         # 24.2 V
        await c.gsa72_set_temp_c(1850)        # 18.5°C → 18°C / 64°F

asyncio.run(demo())
```

On dev laptop without GPIO, use `port="/dev/null"` with mocked tests, or `socat` pty. See `tak-bridge-chronometer/README_chronometer.md:8` for full API table and `encode_packet` inspection.

---

## 8. Service Internals (Summary)

Deep dive: `tak-bridge-chronometer/README_main.md:1`.

```
main() ──► connect() on /dev/serial0 @38400 ──► sync_loop() ──► sync_once()
                                                │                ├─ datetime.now().astimezone().hour → set_local
                                                │                └─ datetime.now(timezone.utc) h,m,s → set_utc_hms
                                                │                every 3600 s, health True
                                                └─ on exception: health False after 3 fails, backoff 1→60 s, retry
```

- `sync_once` `__main__.py:52` – 2 commands with `0.05 s` gap, 2 ms/byte already in library
- `sync_loop` `__main__.py:87` – `while True: try sync_once except → HEALTH_MAX_ERRORS`
- `main` `__main__.py:114` – dotenv, logging, `HEALTH_FILE` override, `SIGINT/TTERM` → `stop_event`, exponential backoff, `add_signal_handler`; warns if `CHRONO_PORT` not `serial0`/`ttyAMA0`/`ttyS0`
- `run` `__main__.py:228` – `asyncio.run(main())` for poetry

Time source: Pi system clock (NTP). No internal drift correction; hourly resync keeps gauge within seconds (gauge may drift ~1 min/hour if un-synced, negligible for hourly read).

---

## 9. Configuration Reference (Consolidated)

| Var | Default | Docs | Notes |
|-----|---------|------|-------|
| `CHRONO_PORT` / `SERIAL_PORT` | `/dev/serial0` | `README_main.md:4` | **GPIO UART** – `serial0` → `ttyAMA0` (Pi4/5) or `ttyS0` (Pi Zero/3). NOT USB. |
| `CHRONO_BAUDRATE` / `BAUDRATE` | `38400` | `README_main.md:4` | FI fixed |
| `UPDATE_INTERVAL` | `3600` | `README_main.md:4` | Seconds; `0` not recommended (busy loop) |
| `HEALTH_FILE` | `/tmp/tak-chronometer-healthy` | `README_main.md:4` | Remove to report healthy |
| `HEALTH_MAX_ERRORS` | `3` | `README_main.md:4` | Consecutive failures → unhealthy |
| `DEBUG` | `0` | `README_main.md:4` | `1` → DEBUG |

---

## 10. File Map

```
takpi/
  README.md                         ← monorepo index (lists this file)
  README_chronometer.md             ← this file (top-level chronometer doc)
  AGENTS.md:84                      ← inventory + chronometer details (GPIO)
  tak-bridge-chronometer/
    README.md                       ← bridge overview (links to submodules)
    README_chronometer.md           ← library deep dive (protocol + API, GPIO)
    README_main.md                  ← service deep dive (sync + systemd, GPIO)
    pyproject.toml:1                ← deps, scripts.start, tool configs
    src/tak_bridge_chronometer/
      __init__.py:1                 ← re-exports ChronometerClient
      chronometer.py:1              ← library (encode_packet + ChronometerClient, GPIO)
      __main__.py:1                 ← service (sync_once/loop/main/run, serial0)
    tests/
      test_chronometer.py:1         ← 7 unit tests (encode + mocked serial + sync_once)
    systemd/
      tak-bridge-chronometer.service:1
```

---

## 11. Development

```bash
# common (if needed later)
poetry install --directory common

# per bridge (GPIO UART, no USB)
poetry install --directory tak-bridge-chronometer
poetry run --directory tak-bridge-chronometer black . && poetry run --directory tak-bridge-chronometer mypy src && poetry run --directory tak-bridge-chronometer pylint src
poetry run --directory tak-bridge-chronometer pytest -v
```

CI uses venv per project (`poetry` `.venv`), never `--break-system-packages` (per user request).

**GPIO vs USB note:** If you *do* use a USB adapter for bench testing, set `CHRONO_PORT=/dev/ttyUSB0` – service will log a warning but still work (library is port-agnostic). Production on Pi **must** be `serial0`.

---

## 12. Troubleshooting (Condensed)

| Problem | Check | Fix |
|---------|-------|-----|
| `Permission denied /dev/serial0` | `dialout` | `sudo usermod -a -G dialout pi` + relogin; `ls -l /dev/serial0` |
| `No such file /dev/serial0` | UART not enabled | `raspi-config` enable serial, remove `console=serial0` from `cmdline.txt`, disable `serial-getty@serial0`, reboot; `dmesg \| grep uart` |
| Gauge ignores `set_local` | Wrong `ID` (must 109) or baud | Verify `GSA72_ID` and `38400`, check wiring pin 8→Pin10, GND continuity, 3.3V level |
| Time off by 1 h | Pi TZ | `timedatectl set-timezone Europe/Helsinki; date` |
| UTC off | NTP | `timedatectl status | grep NTP; date -u` |
| Health file present | 3 fails | `journalctl -u tak-bridge-chronometer` – likely serial error |
| `by-id` warnings | Using old `.env` | Change `.env` to `CHRONO_PORT=/dev/serial0` – GPIO is canonical for this bridge |

Full matrix: `tak-bridge-chronometer/README_main.md:8` and `README_chronometer.md:11`.

---

## 13. Changelog & Future

| Version | Date | Notes |
|---------|------|-------|
| 0.1.0 | 2026-09-07 | Initial: library `chronometer.py` + service `__main__.py` hourly sync via **GPIO UART `serial0`**, systemd, health, tests |

**Planned extensions:** wall-clock alignment (sleep to `00:00`), `FLT`/`VOLT`/`TEMP` periodic polling (`vcgencmd`, INA219), `--once` CLI, Docker per-bridge, `common/` shared health/dotenv.

---

## 14. References

- Bridge: `tak-bridge-chronometer/README.md:1`
- Library: `tak-bridge-chronometer/README_chronometer.md:1` + `src/tak_bridge_chronometer/chronometer.py:1`
- Service: `tak-bridge-chronometer/README_main.md:1` + `src/tak_bridge_chronometer/__main__.py:1`
- Tests: `tak-bridge-chronometer/tests/test_chronometer.py:1`
- C++ source: `~/Dev/arduIllusion/ArduIllusion/ArduIllusion.h:44` (`GSA72_ID`) + `h:64` + `cpp:32` + `cpp:161` + `driver.py:237`
- Manual: `~/Dev/arduIllusion/Fight-Illusion-manual-2016.pdf` + `arduIllusion/README.md:17` (pinout) – **Pi GPIO mapping adds `pin 8→GPIO14`, `pin10→GPIO15`**
- Monorepo: `README.md:1` + `AGENTS.md:1`

