# README_main – Hourly Sync Service Submodule

> **Path:** `takpi/tak-bridge-chronometer/src/tak_bridge_chronometer/__main__.py:1`  
> **Library used:** `chronometer.py:1` (`README_chronometer.md:1`) – `ChronometerClient` on ` /dev/serial0`  
> **Entry:** `poetry run start` → `tak_bridge_chronometer.__main__:run` (`pyproject.toml:32`)  
> **Health:** `/tmp/tak-chronometer-healthy` (absent = healthy)  
> **Interface:** **Pi GPIO UART** (`GPIO14` pin 8 TXD → gauge RxD, `GPIO15` pin 10 RXD ← gauge TxD, `/dev/serial0` @38400)

This document is the thorough reference for the **service submodule** that keeps the Flight Illusion GSA-72 Chronometer synchronized to the Raspberry Pi system time every hour **via the Pi's GPIO hardware UART**.

---

## 1. Purpose & Requirements

The GSA-72 has no internal RTC battery backup that survives Pi reboots gracefully, and it drifts. The service ensures:

1. **Local time** (`gsa72_set_local`) shows the Pi's local hour (with timezone/DST as configured via `timedatectl`).
2. **UTC time** (`gsa72_set_utc_hms`) shows the Pi's UTC H:M:S (authoritative, via NTP/`systemd-timesyncd` or `chrony`).
3. Both are refreshed **every hour** (`UPDATE_INTERVAL=3600` default) without manual intervention, surviving GPIO UART glitches and reboots via `systemd` and exponential backoff.

**User requirement (original):** *"create a 'main' python script with an async function which sets local time and UTC of the Chrono every hour from the Pi's own time."* – Implemented as `sync_once()` `__main__.py:52` + `sync_loop()` `__main__.py:87` + `main()` reconnect loop `__main__.py:114`. Updated to use **Pi GPIO serial (`/dev/serial0`)**, not USB-FTDI.

**Non-requirements:** No TAK COT, no flight timer/voltage/temp periodic updates (available via library if needed).

---

## 2. Architecture

```
                ┌─────────────────────────────────┐
                │  systemd tak-bridge-chronometer │
                │  After=network.target           │
                └──────────────┬──────────────────┘
                               │ ExecStart: poetry run start (GPIO UART)
                               ▼
                 ┌─────────────────────────┐
                 │  __main__.py:run()      │  sync wrapper → asyncio.run(main())
                 └────────────┬────────────┘
                              ▼
                 ┌─────────────────────────┐
                 │  main()  __main__.py:114│  load_dotenv, logging, signal, backoff loop
                 └────────────┬────────────┘
                              │ await ChronometerClient(port="/dev/serial0").connect() (2 s settle, GPIO14/15)
                              ▼
                 ┌─────────────────────────┐
                 │  sync_loop(interval)    │  __main__.py:87  while True:
                 └────────────┬────────────┘     try: sync_once() → _report_health(True)
                              │                  except: _report_health(False)
                              │                  sleep(interval)
                              ▼
                 ┌─────────────────────────┐
                 │  sync_once(client)      │  __main__.py:52
                 └────────────┬────────────┘
                  ┌───────────┴───────────┐
                  ▼                       ▼
         now_local = datetime           now_utc = datetime
         .now().astimezone()            .now(timezone.utc)
                  │                       │
                  ▼                       ▼
         client.gsa72_set_local ──0.05s──► client.gsa72_set_utc_hms
         (local_hour 0-23)                (utc_h,m,s)
                  │
                  ▼  GPIO14 pin 8 @38400  TTL 3.3V
         ChronometerClient._send_command → encode_packet → pyserial on /dev/serial0 → 6-byte frame +2ms/byte
```

### 2.1 Key Functions

| Function | Line | Role |
|----------|------|------|
| `sync_once(client)` | `__main__.py:52` | One-shot sync: reads Pi time, calls `gsa72_set_local` (GPIO14) + (`sleep 0.05`) + `gsa72_set_utc_hms` on `/dev/serial0`, logs |
| `sync_loop(client, interval)` | `__main__.py:87` | Infinite loop wrapping `sync_once` with `HEALTH_MAX_ERRORS` tracking and `sleep(interval)` |
| `main()` | `__main__.py:114` | Loads env (`/dev/serial0` default), sets up logging/signal/backoff, creates `ChronometerClient`, handles GPIO connect/disconnect and `stop_event` |
| `run()` | `__main__.py:228` | Sync wrapper for `poetry run start` (`asyncio.run(main())`) |
| `_report_health(ok)` | `__main__.py:40` | Writes/removes `/tmp/tak-chronometer-healthy` (same pattern as `tak-feeder-traffic-fin-roads:feeder.py:23`) |

### 2.2 Reconnect & Backoff (`__main__.py:165`)

```
connect() on /dev/serial0 ──► success → backoff=1, health=True, create sync_loop task
    │                               wait FIRST_COMPLETED(sync_loop, stop_event)
    │                               ├─ stop_event → cancel sync_loop, disconnect, return
    │                               └─ sync_loop exit (exception) → raise
    └─ exception/timeout → health=False, disconnect, wait backoff or stop_event, backoff*=2 (max 60s), retry
```

Handles: `Permission denied /dev/serial0`, UART not enabled (`No such file`), GPIO wiring error, serial exception, `CancelledError`. Warns if `CHRONO_PORT` is not `serial0`/`ttyAMA0`/`ttyS0` (e.g., leftover USB `ttyUSB0`).

### 2.3 Signal Handling (`__main__.py:149`)

`SIGINT`/`SIGTERM` → `stop_event.set()` → graceful cancel of `sync_loop` + `disconnect()` on GPIO. Works on Pi (Linux); `NotImplementedError` ignored on Windows.

---

## 3. Time Handling

### 3.1 Local Time

```python
now_local = datetime.datetime.now().astimezone()
local_hour = now_local.hour  # 0-23, respects /etc/timezone & DST
```

- Uses Pi's configured timezone (`timedatectl set-timezone Europe/Helsinki` etc.).
- `tzname()` logged for audit.
- Only hour is sent because `gsa72_setLocal(byte h)` (`ArduIllusion.cpp:185`) drives a 24h analog/digital hour display; minutes are not shown in local window (per GSA-72 manual).

### 3.2 UTC Time

```python
now_utc = datetime.datetime.now(datetime.timezone.utc)
utc_h, utc_m, utc_s = now_utc.hour, now_utc.minute, now_utc.second
```

- Always UTC, independent of local TZ.
- Seconds since midnight computed inside `gsa72_set_utc_hms` as `h*3600+m*60+s` (see library `chronometer.py:229`).
- Wrap `>65535` handled in library (65535 = 18:12:15, max encodable; later times wrap to negative – preserved from C++).

### 3.3 NTP Dependency

Service assumes Pi time is correct via `systemd-timesyncd`, `chrony`, or `ntpd` on the GPIO host. No internal NTP query; if Pi is unsynced, gauge will be unsynced. Monitor via `timedatectl status` and `journalctl -u systemd-timesyncd`.

### 3.4 Interval & Alignment

- Default `UPDATE_INTERVAL=3600` → every hour from service start on GPIO.
- Not aligned to wall-clock hour boundary (e.g., start at 12:07 → syncs 12:07, 13:07 …). To align to top-of-hour, set `UPDATE_INTERVAL=3600` and restart service at 00:00 or modify to sleep until next hour (future enhancement). Current simple `sleep(interval)` is predictable and testable.
- For tighter sync (e.g., every 5 min for validation), set `UPDATE_INTERVAL=300`.

---

## 4. Configuration (Env / .env)

All vars loaded via `python-dotenv` (`load_dotenv()` `__main__.py:116`).

| Var | Default | Source Line | Description |
|-----|---------|-------------|-------------|
| `CHRONO_PORT` | `/dev/serial0` | `__main__.py:118` | **Pi GPIO UART** device – canonical alias for `ttyAMA0` (Pi 4/5) or `ttyS0` (Pi Zero/3). **Do not use `ttyUSB0`/`by-id` – that's for USB adapters.** See `AGENTS.md:96` & `README_chronometer.md:2.1` for enable steps |
| `SERIAL_PORT` | (fallback) | `__main__.py:118` | Alias for `CHRONO_PORT` (compatibility with old USB configs) |
| `CHRONO_BAUDRATE` | `38400` | `__main__.py:119` | Must be 38400 for FI gauges on GPIO |
| `BAUDRATE` | (fallback) | `__main__.py:119` | Alias |
| `UPDATE_INTERVAL` | `3600` | `__main__.py:120` | Seconds between `sync_once` calls (int) |
| `HEALTH_FILE` | `/tmp/tak-chronometer-healthy` | `__main__.py:36` + `__main__.py:125` | Path for health flag. Absent = healthy (consistent with `AGENTS.md:14`) |
| `HEALTH_MAX_ERRORS` | `3` | `__main__.py:37` | Consecutive failures before marking unhealthy |
| `DEBUG` | `0` | `__main__.py:121` | `1` → `logging.DEBUG`, else `INFO` |

**Precedence:** `CHRONO_PORT` overrides `SERIAL_PORT`; `CHRONO_BAUDRATE` overrides `BAUDRATE`; `HEALTH_FILE` env overrides `__main__.py:36` default at runtime (`__main__.py:125`).

### 4.1 `.env` Examples

**Minimal (GPIO, production)**

```
CHRONO_PORT=/dev/serial0
CHRONO_BAUDRATE=38400
UPDATE_INTERVAL=3600
HEALTH_FILE=/tmp/tak-chronometer-healthy
```

**Verbose/debug, every 5 min on GPIO**

```
CHRONO_PORT=/dev/serial0
UPDATE_INTERVAL=300
DEBUG=1
```

**Bench-test with USB adapter (warns but works)**

```
# Only for temporary bench testing; production must be GPIO
CHRONO_PORT=/dev/ttyUSB0
UPDATE_INTERVAL=10
DEBUG=1
```

**File location:** `tak-bridge-chronometer/.env` (not committed, ignored via `.gitignore`). `systemd` loads it via `EnvironmentFile=` (see §7).

### 4.2 Enable GPIO UART – One-Time Pi Setup

```bash
# raspi-config → Interface → Serial: disable login shell, enable hardware
# /boot/firmware/config.txt: enable_uart=1, dtoverlay=disable-bt (Pi3/4)
# cmdline.txt: remove console=serial0, console=ttyAMA0
sudo systemctl disable --now serial-getty@serial0.service
sudo reboot
ls -l /dev/serial0  # → ttyAMA0 or ttyS0
groups  # must include dialout
```

See `README_chronometer.md:2.1` and top-level `../../README_chronometer.md:3` for full wiring: Pi pin 8 (GPIO14 TXD) → gauge Pin 10 RxD, pin 10 (GPIO15 RXD) ← gauge Pin 8 TxD (optional), GND common.

---

## 5. Running

### 5.1 Prerequisites

- Python 3.12+, Poetry 2.0+
- `pyserial` + `python-dotenv` (installed via `poetry install`)
- **GPIO UART enabled** (see §4.2): `/dev/serial0` exists and is not a getty; `ls -l /dev/serial0` should be `crw-rw---- dialout`
- Serial permissions: `sudo usermod -a -G dialout $USER` + relogin. Verify: `groups` includes `dialout`; `ls -l /dev/serial0` readable.
- Wiring: Pi pin 8 → gauge Pin10, Pi GND → gauge GND (Pins 5-7/9), external +12V/+5V PSU for gauge (not Pi 5V)
- Pi time synced: `timedatectl` should show `NTP synchronized: yes`

### 5.2 Install

```bash
git clone <takpi> && cd takpi
poetry install --directory tak-bridge-chronometer
# or with dev tools
poetry install --directory tak-bridge-chronometer --with dev
```

Poetry creates `.venv` inside bridge (isolated, respects venv requirement).

### 5.3 Run (foreground, for validation)

```bash
# with .env containing CHRONO_PORT=/dev/serial0
poetry run --directory tak-bridge-chronometer start
# equivalent
poetry run --directory tak-bridge-chronometer python -m tak_bridge_chronometer
# quick one-shot validation (service will loop hourly; Ctrl-C to stop)
CHRONO_PORT=/dev/serial0 poetry run --directory tak-bridge-chronometer start
# benchtest fallback (warns):
CHRONO_PORT=/dev/ttyUSB0 poetry run --directory tak-bridge-chronometer start
```

Expected log (GPIO):

```
2026-09-07 12:00:00 INFO __main__: tak-bridge-chronometer starting – port=/dev/serial0 baud=38400 interval=3600s
2026-09-07 12:00:02 INFO chronometer: Connecting to GSA-72 Chronometer on /dev/serial0 @38400
2026-09-07 12:00:04 INFO __main__: Connected to Chronometer, starting hourly sync
2026-09-07 12:00:04 INFO __main__: Syncing Chronometer – local 14:00 (local EEST) UTC 11:00:04
```

Gauge should show local hour (14) and UTC (11:00:04). Scope GPIO14 pin 8 @38400 to see `00 6D ... FF` frames. Wait 1 h or set `UPDATE_INTERVAL=10` for quick test.

### 5.4 Health Check

```bash
cat /tmp/tak-chronometer-healthy  # no file = healthy, file exists with "unhealthy" = failure
# After 3 consecutive sync failures (e.g., /dev/serial0 permission), file appears; cleared on next success
```

Same pattern as `tak-feeder-traffic-fin-roads` health.

### 5.5 Without Hardware (CI / dev laptop)

All logic except `serial.Serial` on `/dev/serial0` is unit-tested with mocks. Run:

```bash
poetry run --directory tak-bridge-chronometer pytest -v  # 7 tests, no hardware, no GPIO
poetry run --directory tak-bridge-chronometer python -c "from tak_bridge_chronometer.chronometer import encode_packet; print(encode_packet(109,5,45296).hex(' '))"
```

For GPIO-less dev, tests mock `serial.Serial` and `asyncio.sleep` – no `/dev/serial0` required.

---

## 6. Logging

- Format: `%(asctime)s %(levelname)s %(name)s: %(message)s` (`__main__.py:128`)
- Level: `DEBUG` if `DEBUG=1` else `INFO`
- Logger names: `__main__` (service) and `tak_bridge_chronometer.chronometer` (library, logs `Connecting to GSA-72 Chronometer on /dev/serial0`)
- `journalctl` integration when run via systemd (see §7)

**DEBUG example**

```
DEBUG chronometer: Connecting to GSA-72 Chronometer on /dev/serial0 @38400...
DEBUG __main__: Syncing Chronometer – local 14:00 (local EEST) UTC 11:00:04 on GPIO14
```

---

## 7. Systemd Deployment (Pi, GPIO)

**Unit file:** `systemd/tak-bridge-chronometer.service:1`

```ini
[Unit]
Description=TAK Bridge – Flight Illusion GSA-72 Chronometer (hourly time sync via GPIO UART)
After=network.target

[Service]
Type=simple
User=pi
Group=dialout
WorkingDirectory=/home/pi/Dev/TAK/takpi/tak-bridge-chronometer
EnvironmentFile=/home/pi/Dev/TAK/takpi/tak-bridge-chronometer/.env  # must contain CHRONO_PORT=/dev/serial0
ExecStart=/home/pi/.local/bin/poetry run start
Restart=always
RestartSec=5
ExecStartPre=/bin/sh -c 'rm -f /tmp/tak-chronometer-healthy'

[Install]
WantedBy=multi-user.target
```

### 7.1 Install Steps

```bash
# 0. Enable UART first (see §4.2) and verify /dev/serial0 exists
ls -l /dev/serial0 && dmesg | grep uart

# 1. Ensure .env exists with GPIO port
cat tak-bridge-chronometer/.env  # CHRONO_PORT=/dev/serial0

# 2. Install service
sudo cp tak-bridge-chronometer/systemd/tak-bridge-chronometer.service /etc/systemd/system/
sudo systemctl daemon-reload

# 3. Enable & start
sudo systemctl enable --now tak-bridge-chronometer

# 4. Verify
systemctl status tak-bridge-chronometer
journalctl -u tak-bridge-chronometer -f
cat /tmp/tak-chronometer-healthy || echo "healthy"
# scope GPIO14 pin 8: should see 6-byte frames @38400 every hour (or 10s if UPDATE_INTERVAL=10)
```

### 7.2 Common Pitfalls (GPIO)

| Error | Cause | Fix |
|-------|-------|-----|
| `Failed to start: EnvironmentFile not found` | Missing `.env` | `touch .env` or create with `CHRONO_PORT=/dev/serial0` |
| `SerialException: [Errno 13] Permission denied /dev/serial0` | User not in `dialout` or service `Group=` wrong | `sudo usermod -a -G dialout pi`; check `User=`/`Group=`; `ls -l /dev/serial0` |
| `SerialException: could not open port /dev/serial0: No such file` | UART not enabled or `console=serial0` still in `cmdline.txt` | `raspi-config` enable UART, remove console, `dtoverlay=disable-bt`, disable `serial-getty@serial0`, reboot; `ls -l /dev/serial0` |
| `Unhealthy` file persists, gauge silent | 3 consecutive sync failures (GPIO wiring/baud) | `journalctl -u tak-bridge-chronometer` for exception; scope GPIO14 pin 8 @38400; check wiring: Pi pin 8 → gauge Pin10, GND continuity, 38400, external PSU |
| `Using /dev/ttyUSB0 – ... GPIO UART` warning | Old `.env` with USB path | Update `.env` to `CHRONO_PORT=/dev/serial0` – GPIO is canonical for this bridge (USB only for bench) |
| Time wrong by 1 h | DST mismatch, Pi timezone not set | `sudo timedatectl set-timezone Europe/Helsinki`; `timedatectl` |
| `serial-getty` still holds `/dev/serial0` | Getty not disabled | `sudo systemctl disable --now serial-getty@serial0`; verify `lsof /dev/serial0` empty |

### 7.3 Monitoring

```bash
# watch health (GPIO)
watch -n 1 cat /tmp/tak-chronometer-healthy || echo "healthy on /dev/serial0"

# uptime & restarts
systemctl show tak-bridge-chronometer --property=ActiveEnterTimestamp --property=NRestarts

# logs last hour (filter GPIO)
journalctl -u tak-bridge-chronometer --since "1 hour ago" | grep -E "serial0|GPIO|Syncing"

# check UART
ls -l /dev/serial0 && stty -F /dev/serial0 -a  # should show 38400
```

---

## 8. Troubleshooting Matrix (GPIO)

| Symptom | Check | Command |
|---------|-------|---------|
| Gauge not moving on GPIO | Serial open? `pyserial` version, baud, wiring Pin 8→Pin10, GND | `python -c "import serial; print(serial.VERSION)"`; `ls -l /dev/serial0`; scope GPIO14 pin 8 @38400 for `00 6D ... FF` |
| Local hour off | Pi local time | `date; timedatectl` |
| UTC off | Pi UTC, NTP sync | `date -u; timedatectl status | grep NTP` |
| Service restarts every 5 s | `Restart=always` + GPIO connection error | `journalctl -u tak-bridge-chronometer | tail -n 50` – likely `/dev/serial0` permission/UART |
| Health file present | Consecutive errors >=3 on `/dev/serial0` | `cat /tmp/tak-chronometer-healthy; journalctl -u tak-bridge-chronometer` |
| Intermittent GPIO glitch | `dmesg` shows `uart` overrun | Check wire length <30 cm, twist GND, ensure PSU ground common, no BT conflict (`dtoverlay=disable-bt`) |
| Warning about `ttyUSB0` | Old config | Update `.env` to `CHRONO_PORT=/dev/serial0` |

---

## 9. Testing

### 9.1 Unit Tests `tests/test_chronometer.py:11`

- `test_sync_once_calls_both_setters` verifies `sync_once` `__main__.py:52` calls `gsa72_set_local` once with `0-23` and `gsa72_set_utc_hms` once with valid `h,m,s` (GPIO mocked).

Run (no GPIO hardware needed):

```bash
poetry run --directory tak-bridge-chronometer pytest -v
# 7 passed (encode + GPIO mocked serial + sync_once)
```

### 9.2 Formatting & Types

```bash
poetry run --directory tak-bridge-chronometer black --check src tests
poetry run --directory tak-bridge-chronometer mypy src  # strict
poetry run --directory tak-bridge-chronometer pylint src  # 10.00
```

### 9.3 Manual Hardware Test (GPIO)

```bash
# Enable UART, wire Pi pin 8 → gauge Pin10
UPDATE_INTERVAL=10 CHRONO_PORT=/dev/serial0 poetry run --directory tak-bridge-chronometer start
# observe gauge: should update every 10 s, local hour + UTC seconds ticking
# scope GPIO14 pin 8: 38400 8N1 frames
```

Bench fallback (USB, warns):

```bash
UPDATE_INTERVAL=10 CHRONO_PORT=/dev/ttyUSB0 poetry run --directory tak-bridge-chronometer start
```

---

## 10. Performance & Resources

- CPU: negligible (<1% on Pi 4, mostly idle `sleep` on GPIO UART)
- Memory: ~20 MB (Python + pyserial on `/dev/serial0`)
- Serial bandwidth: 6 bytes * ~2 commands per hour on GPIO14 @38400 → trivial
- Backoff: avoids busy loop on GPIO failure (1→60 s)

---

## 11. Security & Pi Specifics (GPIO)

- No network, no TAK mTLS – no certs needed.
- `.env` contains only `CHRONO_PORT=/dev/serial0`, not secrets; still excluded via `.gitignore`.
- Runs as `pi`/`dialout`, not root; `dialout` owns `/dev/serial0`.
- No `sudo` needed for service `Restart`.
- **GPIO UART is exclusive** – do not run other services on `/dev/serial0` (e.g., `serial-getty`, `pigpiod` UART). Disable `serial-getty@serial0` and ensure `enable_uart=1`.
- For other bridges using USB, `AGENTS.md:96` still prefers `/dev/serial/by-id/` – but this bridge is **GPIO-only** by design.

---

## 12. Future Extensions

- **Align to wall-clock:** Sleep until next `00:00` then `3600` on GPIO.
- **Flight timer:** Expose `gsa72_set_flt_hms` via env `FLT_START` or mission trigger on GPIO.
- **Temp/Volt telemetry:** Periodically read Pi `vcgencmd measure_temp` / INA219 and call `gsa72_set_temp_c` / `gsa72_set_volt` over same `/dev/serial0`.
- **One-shot mode:** CLI flag `--once` to sync and exit (useful for `cron` alternative, still GPIO).
- **Docker:** One-container-per-bridge with `--device=/dev/serial0` (see `AGENTS.md:14`).

---

## 13. References

- Library doc: `README_chronometer.md:1` (protocol, API, GPIO wiring)
- Bridge overview: `README.md:1`
- Top-level doc: `../../README_chronometer.md:1` and `../../README.md:1` (GPIO)
- Source: `__main__.py:1` + `chronometer.py:1` (both GPIO UART `/dev/serial0`)
- Systemd: `systemd/tak-bridge-chronometer.service:1`
- Original C++: `~/Dev/arduIllusion/ArduIllusion/ArduIllusion.cpp:52` (`sync_once` mapping), `ArduIllusion.cpp:185`, `ArduIllusion.cpp:168` – Arduino `Serial3` → Pi `serial0`
- Manual: `~/Dev/arduIllusion/Fight-Illusion-manual-2016.pdf` + Pi UART docs (`raspi-config`, `config.txt`, `cmdline.txt`)
- Health pattern: `tak-feeder-traffic-fin-roads:feeder.py:23`

---

## 14. Changelog

| Version | Date | Notes |
|---------|------|-------|
| 0.1.0 | 2026-09-07 | Initial hourly sync service – `sync_once`+`sync_loop`+`main` with GPIO UART `/dev/serial0` (pin 8/10), backoff and health |

