# tak-bridge-chronometer

Raspberry Pi bridge for the **Flight Illusion GSA-72 Chronometer** (Davtron-style GA clock) via **GPIO UART**.
Syncs the gauge's **local hour** and **UTC H:M:S** from the Pi's system time every hour over `/dev/serial0` (GPIO14/15, 38400 baud, 3.3V TTL).

- Library: `tak_bridge_chronometer.chronometer` – Python port of `ArduIllusion` Chrono functions (`ArduIllusion.cpp:161`, `ArduIllusion.h:64`) on GPIO UART
- Service: `tak_bridge_chronometer.__main__` – `asyncio` loop on `/dev/serial0` with reconnect + health file

## Detailed Submodule Documentation – `README_<topic>.md`

> Per project rule every submodule gets a thorough `README_<topic>.md`. This bridge has **2 Python submodules** → 2 dedicated docs (plus monorepo aggregation `../../README_chronometer.md:1` and index `../../README.md:15`). All GPIO-based.

| # | Topic | File | Covers |
|---|-------|------|--------|
| 1 | **chronometer library** | [`README_chronometer.md`](README_chronometer.md) | `src/tak_bridge_chronometer/chronometer.py:1` – C++ mapping, 6-byte protocol on `GPIO14` TXD, `encode_packet` + `ChronometerClient` (`gsa72_set_*`) via `/dev/serial0`, async + lock, bug fix, examples, mocked GPIO tests |
| 2 | **main service** | [`README_main.md`](README_main.md) | `src/tak_bridge_chronometer/__main__.py:1` – `sync_once()` (`__main__.py:52`), `sync_loop()` (`__main__.py:87`), `main()` GPIO UART (`__main__.py:114`, `/dev/serial0`), time handling (local vs UTC, DST/NTP), env table, systemd on GPIO, health, monitoring, troubleshooting |
| 3 | **top-level aggregation** | [`../../README_chronometer.md`](../../README_chronometer.md) | Monorepo-level overview: hardware + GPIO wiring + protocol summary + install + quick start + file map, links to this bridge |

Top-level index also lists them: `../../README.md:15` (table rows 2–3).

## Hardware (GPIO UART)

- Flight Illusion GSA-072: https://www.flightillusion.com/military-vintage/chrono-gauges-mil/gsa-072-chronometerclock-davtron-style/
- Interface: **TTL UART 3.3V via Pi GPIO UART** (`GPIO14` TXD header pin 8 → gauge `RxD` Pin 10, `GPIO15` RXD pin 10 ← gauge `TxD` Pin 8 optional, GND common) at **38400 baud** (`/dev/serial0` → `ttyAMA0`/`ttyS0`)
- Gauge ID: `109` (`GSA72_ID`), commands: `SETLOCAL=4`, `SETUTC=5`, `SETFLT=6`, `SETVOLT=9`, `SETTEMPC=11`, `SETTEMPF=12`
- Wiring: Pi GPIO, not USB – see `README_chronometer.md:2.1` and `../../README_chronometer.md:3` for full diagram + enable steps (`raspi-config` enable UART, `dtoverlay=disable-bt`, remove `console=serial0` from `cmdline.txt`, `systemctl disable serial-getty@serial0`)
- No USB `by-id` path; production is GPIO. Bench fallback `ttyUSB0` works but warns.

Protocol: 6-byte packet `[0x00, id, cmd<<4|flags, data_low|0x01, data_high|0x02, 0xFF]` with 2 ms per-byte gap (`ArduIllusion.cpp:32` `sendCommand`) on `GPIO14` TXD.

## Wiring (Pi GPIO → Gauge, Condensed)

```
Pi Pin 8  GPIO14 TXD ───────────── Gauge Pin10 RxD (Fi)
Pi Pin10  GPIO15 RXD ───────────── Gauge Pin8  TxD (optional)
Pi GND (6/9/14/20/25/30/34/39) ─── Gauge Pins 5-7,9 GND (common)
Gauge +12V/+5V (Pins1-4) ─── External PSU (not Pi 5V)
```

Enable: `raspi-config` → Serial → No login shell / Yes hardware → `enable_uart=1` + `dtoverlay=disable-bt` (Pi3/4) → remove `console=serial0` → reboot → `ls -l /dev/serial0`.

## Library – ChronometerClient (GPIO)

Ported C++ → Python (async `pyserial` on `/dev/serial0`):

| C++ (ArduIllusion.cpp) | Python (chronometer.py) on GPIO |
|---|---|
| `gsa72_setUTC(long sec)` | `ChronometerClient(port="/dev/serial0").gsa72_set_utc(seconds)` |
| `gsa72_setUTC(h,m,s)` | `ChronometerClient.gsa72_set_utc_hms(h,m,s)` |
| `gsa72_setFLT(long sec)` | `ChronometerClient.gsa72_set_flt(seconds)` |
| `gsa72_setFLT(h,m,s)` | `ChronometerClient.gsa72_set_flt_hms(h,m,s)` |
| `gsa72_setLocal(byte h)` | `ChronometerClient.gsa72_set_local(hours)` |
| `gsa72_setTempC(int dc)` | `ChronometerClient.gsa72_set_temp_c(decicelsius)` |
| `gsa72_setVolt(long mV)` | `ChronometerClient.gsa72_set_volt(millivolts)` |

Low-level `encode_packet(id, cmd, value)` is exposed for unit tests (pure replica of `sendCommand`, no GPIO needed).

```python
import asyncio
from tak_bridge_chronometer.chronometer import ChronometerClient

async def main():
    async with ChronometerClient(port="/dev/serial0") as c:  # GPIO14 pin 8
        await c.gsa72_set_local(14)          # local 14:00
        await c.gsa72_set_utc_hms(12, 34, 56)
        await c.gsa72_set_volt(28500)       # 28.5 V
        await c.gsa72_set_temp_c(2150)      # 21.5 °C → 21 °C + 69 °F

asyncio.run(main())
```

Notes:
- `>65535` wrap (`65535 - value`) preserved from C++ for fidelity.
- `set_temp_c` corrects the C++ `9/5` integer-division bug to proper `1.8*C+32`.
- All frames go out `GPIO14` TXD at 38400 with 2 ms/byte gap.

## Service – Hourly Sync (GPIO)

`src/tak_bridge_chronometer/__main__.py:52` `sync_once()` on `/dev/serial0`:

1. `datetime.now().astimezone()` → `local_hour` → `gsa72_set_local`
2. `datetime.now(timezone.utc)` → `H:M:S` → `gsa72_set_utc_hms`
3. Sleep `UPDATE_INTERVAL` (default **3600 s**) → repeat

Main loop (`main()` `__main__.py:114`) handles: GPIO serial connect (`/dev/serial0`, 2 s settle), health reporting, exponential backoff on GPIO error (UART disabled/permission), `SIGINT`/`SIGTERM`. Warns if `CHRONO_PORT` is not GPIO (`serial0`/`ttyAMA0`/`ttyS0`) like leftover USB `ttyUSB0`.

### Env / .env (GPIO)

| Var | Default | Notes |
|---|---|---|
| `CHRONO_PORT` / `SERIAL_PORT` | `/dev/serial0` | **GPIO UART** canonical (→ `ttyAMA0` on Pi4/5 or `ttyS0`). NOT `ttyUSB0`. Enable via `raspi-config`. |
| `CHRONO_BAUDRATE` / `BAUDRATE` | `38400` | FI fixed |
| `UPDATE_INTERVAL` | `3600` | Seconds between syncs |
| `HEALTH_FILE` | `/tmp/tak-chronometer-healthy` | Absence = healthy |
| `HEALTH_MAX_ERRORS` | `3` | Consecutive errors → unhealthy |
| `DEBUG` | `0` | `1` → DEBUG logs |

`.env` example (GPIO production):

```
CHRONO_PORT=/dev/serial0
CHRONO_BAUDRATE=38400
UPDATE_INTERVAL=3600
```

Bench USB fallback (warns, not GPIO):

```
CHRONO_PORT=/dev/ttyUSB0
```

### Run (GPIO)

```bash
# enable UART first (see wiring section), then:
poetry install --directory tak-bridge-chronometer

# dev run on GPIO (needs Pi + gauge on pins 8/10)
poetry run --directory tak-bridge-chronometer start
# or
poetry run --directory tak-bridge-chronometer python -m tak_bridge_chronometer

# logs – should show GPIO port
journalctl -u tak-bridge-chronometer -f
cat /tmp/tak-chronometer-healthy  # no file = healthy
# scope GPIO14 pin 8 @38400 to see frames
```

Without hardware, unit tests mock GPIO serial – no Pi needed: `poetry run --directory tak-bridge-chronometer pytest -v`.

## Systemd (GPIO)

See `systemd/tak-bridge-chronometer.service`. Install on Pi (GPIO):

```bash
sudo cp systemd/tak-bridge-chronometer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tak-bridge-chronometer
```

Service uses `User=pi Group=dialout` and `WorkingDirectory` with `CHRONO_PORT=/dev/serial0`. Ensure `serial-getty@serial0` disabled, UART enabled (see above). Verify `ls -l /dev/serial0` and `dmesg | grep uart`.

## Testing

```bash
poetry run --directory tak-bridge-chronometer pytest -v  # 7 tests, GPIO mocked
poetry run --directory tak-bridge-chronometer black . && poetry run --directory tak-bridge-chronometer mypy src && poetry run --directory tak-bridge-chronometer pylint src
```

## References

- Source library: `~/Dev/arduIllusion/ArduIllusion/ArduIllusion.cpp:161` + `ArduIllusion.h:64` + `ArduIllusion.cpp:32` on GPIO `serial0`
- Python driver reference: `~/Dev/arduIllusion/ArduIllusion/driver.py:237` (FTDI variant – this port is GPIO)
- Manual: `~/Dev/arduIllusion/Fight-Illusion-manual-2016.pdf` + Pi UART (`raspi-config`, `config.txt`, `cmdline.txt`)
- Thorough docs: [`README_chronometer.md`](README_chronometer.md) (library GPIO), [`README_main.md`](README_main.md) (service GPIO), [`../../README_chronometer.md`](../../README_chronometer.md) (monorepo GPIO), index [`../../README.md`](../../README.md)
