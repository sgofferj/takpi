# takpi – Raspberry Pi TAK Hardware Bridge Collection

Monorepo for bridging hardware attached to a Raspberry Pi into the TAK ecosystem.

Each sub-directory is an **independent** Python project (own `pyproject.toml`, `systemd` unit, `README`). Shared helpers live in `common/` (`takpi_common`).

## Bridges

| Bridge | Hardware | Interface | Status | Path |
|---|---|---|---|---|
| tak-bridge-chronometer | Flight Illusion GSA-72 Chronometer (Davtron) | Serial 38400 baud (`/dev/serial0` on GPIO14 TXD / GPIO15 RXD, 3.3V TTL) | ✅ Beta – library + hourly sync service | `tak-bridge-chronometer/` |
| tak-bridge-escpos | ESC/POS Thermal Printer (TTL) | Softserial BCM `tx_pin`/`rx_pin` @9600-115200 via `pigpio` **or** hardware `port` `/dev/serial0`/`/dev/ttyUSB0` | ✅ Implemented – async driver (fat/double, `print_alarm` 911) + `PrintRequest`/`CotReceived`→print via `EventBus` | `tak-bridge-escpos/` |

See `AGENTS.md:90` for agent coordination and `tak-bridge-chronometer/README.md` for wiring/env.

## Shared Framework (`common/` + `python-tak-cot-streaming`) + ESC/POS Bridge

Takpi's new **MCP23017 daisy-chain framework** lives in `common/` (shared `takpi_common` library). It provides:
- **MCP23017 driver** (`driver.py:1`) for 0x20-0x27 on I2C-1, `FakeSMBus` for CI, interrupt support
- **Buttons/encoders/LEDs** (`io.py:1`) with debounce/quadrature/blink, daisy-chained `HardwareManager` (`manager.py:1`, 5 ms poll, 200 Hz)
- **EventBus** (`bus.py:1`) bidirectional: buttons/encoders → main, main → LEDs
- **CotBus** (`cot_bus.py:1`) bridging `takstream.CotStream` (from submodule `python-tak-cot-streaming`, **not pytak**) ↔ EventBus → `CotReceived`/`CotSend`
- **TakPiApp** (`app.py:1`) example main wiring button→CoT and CoT→LED
- **ESC/POS printer** (`tak-bridge-escpos/`) – async driver `escpos.py:1` with **softserial GPIO** (`tx_pin`/`rx_pin` BCM via `pigpio` bit-bang, configurable `baudrate` 9600-115200) or hardware `port`, convenience **fat**/double-height/width, `PrintRequest` on `EventBus`, **911 alarm** `print_alarm` via `CotReceived` (takstream)

All via venv `poetry` (per `AGENTS.md:48`), `black`/`mypy --strict`/`pylint` clean, `pytest` with `FakeSMBus`/`FakeSerial`/`AsyncMock` (no Pi, no TAK server).

See `common/README_mcp23017.md`, `common/README_bus.md`, `common/README_cot.md`, `common/README_app.md`, `tak-bridge-escpos/README_escpos.md`/`README_main.md` and `python-tak-cot-streaming/README.md` for takstream.

## Documentation Index – `README_<topic>.md` per Submodule

> Every submodule created in this monorepo gets a dedicated thorough `README_<topic>.md`. This index lists all of them with paths and scope (as requested).

| # | Topic / Submodule | File | Scope & Contents | Location |
|---|-------------------|------|------------------|----------|
| 1 | **chronometer (top-level bridge)** | [`README_chronometer.md`](README_chronometer.md) | Monorepo-level aggregation: hardware (GSA-072) + wiring + protocol summary + library vs service split + quick start + file map + consolidated config/troubleshooting. Entry point for TAK Pi chronometer. | `tak-bridge-chronometer` |
| 2 | **chronometer library** | [`tak-bridge-chronometer/README_chronometer.md`](tak-bridge-chronometer/README_chronometer.md) | Deep dive on `src/tak_bridge_chronometer/chronometer.py:1` – full C++ mapping (`ArduIllusion.h:64`/`ArduIllusion.cpp:32`/`cpp:161`), 6-byte GPIO UART frame on `GPIO14` TXD (`/dev/serial0` @38400) + bit flags, worked hex examples, constants table, `encode_packet()` + `ChronometerClient` API (`gsa72_set_*`), async lock + 2 ms gap, temp bug fix, 3 GPIO examples, mocked tests | `tak-bridge-chronometer` |
| 3 | **chronometer service** | [`tak-bridge-chronometer/README_main.md`](tak-bridge-chronometer/README_main.md) | Deep dive on `src/tak_bridge_chronometer/__main__.py:1` – `sync_once()` (`__main__.py:52`), `sync_loop()` (`__main__.py:87`), `main()` (`__main__.py:114`) on `GPIO UART /dev/serial0` (pin 8/10) with reconnect/backoff, `run()` wrapper, local vs UTC (`datetime.now().astimezone()`/`timezone.utc`), DST/NTP, env table (`CHRONO_PORT=/dev/serial0`/`UPDATE_INTERVAL`/`HEALTH_FILE`), systemd GPIO deployment, health/logging/monitoring, troubleshooting matrix, testing | `tak-bridge-chronometer` |
| 4 | **MCP23017 daisy-chain** | [`common/README_mcp23017.md`](common/README_mcp23017.md) | Thorough: MCP23017 registers (0x20-0x27, BANK=0), I2C wiring SDA GPIO2/SCL GPIO3, daisy-chain 8×16 GPIOs, `HardwareConfig` (devices/buttons/encoders/leds, poll 5 ms), `FakeSMBus`, `MCP23017` driver, `ButtonState` debounce, `EncoderState` quadrature `ENCODER_TABLE`, `LedRuntime` blink, `HardwareManager` polling + `LedCommand` → `OLAT`, tests with FakeSMBus, troubleshooting `i2cdetect`/`Permission denied` | `common` |
| 5 | **EventBus** | [`common/README_bus.md`](common/README_bus.md) | Central async pub/sub (`bus.py:1`): typed `ButtonEvent`/`EncoderEvent`/`LedCommand`/`CotReceived`/`CotSend`, `subscribe`/`publish`/`wait_for`, wildcard `object`, bidirectional hardware→main and main→hardware, CoT→LED, button→CoT examples, `HardwareManager` + `CotBus` wiring, `TakPiApp` handlers, tests | `common` |
| 6 | **CotStream (takstream)** | [`common/README_cot.md`](common/README_cot.md) | **Not pytak** – `python-tak-cot-streaming` submodule (`../python-tak-cot-streaming`, `takstream.CotStream`/`CotEvent`), `CotBus` bridge (`cot_bus.py:1`) `CotReceived`/`CotSend` on `EventBus`, `CotConfig` host/port/certs/callsign, `CotEvent` factories (`marker`/`sa_report`/`chat_message`/`emergency_alert`), keepalive ping `t-x-c-t`, TLS via `cert`/`key`, submodule `git` setup, path dependency `develop=true`, `TakPiApp` button→CoT and CoT→LED, live tests vs real TAK | `common` + submodule |
| 7 | **TakPiApp main** | [`common/README_app.md`](common/README_app.md) | Example main process (`app.py:1` `TakPiApp` + `CotConfig`): owns `EventBus`, starts `HardwareManager` (MCP chain) + `CotBus` (takstream), subscribes `ButtonEvent`/`EncoderEvent`/`CotReceived`, example handlers `_on_button` (btn_emergency → `CotEvent.emergency_alert` + `LedCommand`), `_on_encoder` → LED, `_on_cot` (emergency blink, chat→LED, team Cyan), startup sequence `start()`/`stop()`, `.env` `TAK_HOST` etc., fake demo and real Pi+TAK demo | `common` |
| 8 | **takstream submodule** | [`python-tak-cot-streaming/README.md`](python-tak-cot-streaming/README.md) | Upstream: streaming-friendly TAK CoT lib (typed `CotEvent`, async `CotStream`, TLS, keepalive, `EventFramer`), git submodule `../python-tak-cot-streaming`, `poetry` dev, live tests via `.env` `TAK_LIVE_*` | `python-tak-cot-streaming` (submodule) |
| 9 | **escpos (top-level bridge)** | [`README_escpos.md`](README_escpos.md) | Monorepo aggregation for escpos: softserial BCM `tx_pin`/`rx_pin` @9600-115200 via `pigpio` vs hardware `port`, ESC/POS bytes `ESC @`/`GS !`/`ESC E`, `Fat`/`double`/`print_alarm` 911 (`callsign`/`lat`/`lon`/`location`), `PrintRequest` on `EventBus`, `CotReceived`→911 auto-print, wiring, `pigpiod`, tests | `tak-bridge-escpos` |
| 10 | **escpos driver** | [`tak-bridge-escpos/README_escpos.md`](tak-bridge-escpos/README_escpos.md) | Driver `src/tak_bridge_escpos/escpos.py:1` – `HardwareSerial`/`SoftSerial` (pigpio `wave_add_serial`)/`FakeSerial`, `PrintRequest`, `EscPosPrinter` async `connect`/`_write`/`print_text`/`fat`/`double_height`/`double_width`/`bold`/`print_alarm`/`cut`, softserial `tx_pin`/`rx_pin` BCM + `baudrate`, `FakePigpio`, 9 tests | `tak-bridge-escpos` |
| 11 | **escpos service** | [`tak-bridge-escpos/README_main.md`](tak-bridge-escpos/README_main.md) | Service `src/tak_bridge_escpos/__main__.py:1` – `EventBus` `PrintRequest`→`handle_print_request`, `CotReceived`→`_handle_cot_print` 911 `print_alarm`+`cut`, `ESCPOS_PORT` vs `ESCPOS_TX_PIN`/`ESCPOS_RX_PIN` + `ESCPOS_BAUDRATE`, `TAK_HOST` `CotBus` (takstream), `HEALTH_FILE`, `pigpiod` systemd, direct vs bus examples, troubleshooting softserial/baund | `tak-bridge-escpos` |

**How to add a new bridge (for agents):**

1. Create directory `tak-bridge-<name>/` with `pyproject.toml`, `src/tak_bridge_<name>/`, `tests/`, `systemd/`, `README.md`
2. Create `README_<name>.md` at *both* levels: `takpi/README_<name>.md` (monorepo aggregation) + `takpi/tak-bridge-<name>/README_<name>.md` (submodule specifics) – be thorough (hardware, protocol, API, env, systemd, wiring, examples, tests, references).
3. If the bridge splits into library + service submodules, create `README_<submodule>.md` per Python submodule (e.g., `README_library.md`, `README_main.md`) as done for chronometer.
4. Append row(s) to this index and to the bridge's own `README.md` index.
5. Update `AGENTS.md:84` hardware inventory and `takpi/README.md:9` bridge table.

Current submodule count: **2 bridges (chronometer 2 + escpos 2) + common framework (4: mcp23017, bus, cot, app) + takstream + 2 top-level aggregations → 11 `README_<topic>.md` files** (listed above).

## Quick start

```bash
# clone arduIllusion reference (read-only)
ls ~/Dev/arduIllusion/ArduIllusion/ArduIllusion.{h,cpp}

# install bridge
poetry install --directory tak-bridge-chronometer
poetry run --directory tak-bridge-chronometer start  # needs serial device

# tests (no hardware)
poetry run --directory tak-bridge-chronometer pytest -v
```

## Layout

```
takpi/
  AGENTS.md                         # coordination (now mentions takstream, MCP23017, escpos softserial)
  README.md                         # this file (index of 11 README_<topic>.md)
  README_chronometer.md             # thorough top-level chronometer doc (README_<topic>.md #1)
  README_escpos.md                  # thorough top-level escpos doc (README_<topic>.md #9, softserial GPIO + 911)
  python-tak-cot-streaming/         # git submodule ../python-tak-cot-streaming (takstream, not pytak)
    src/takstream/                  # cot.py (CotEvent), stream.py (CotStream), __init__.py
  common/
    README_mcp23017.md              # MCP23017 daisy-chain framework (README_<topic>.md #4)
    README_bus.md                   # EventBus bidirectional (README_<topic>.md #5)
    README_cot.md                   # CotStream/takstream via CotBus (README_<topic>.md #6)
    README_app.md                   # TakPiApp main wiring (README_<topic>.md #7)
    pyproject.toml                  # takpi-common + path dep python-tak-cot-streaming develop
    src/takpi_common/
      __init__.py
      bus.py                        # EventBus
      config.py / health.py
      app.py                        # TakPiApp + CotConfig
      cot_bus.py                    # CotBus (CotReceived/CotSend)
      mcp23017/
        driver.py                   # MCP23017 + FakeSMBus
        io.py                       # Button/Encoder/Led configs & events
        manager.py                  # HardwareManager (poll 5ms, daisy-chain)
    examples/
      panel_demo.py                 # fake hardware + fake CoT demo (no Pi, no TAK)
    tests/
      test_bus.py / test_driver.py / test_manager.py / test_cot_bus.py / test_app.py
  tak-bridge-chronometer/
    README.md                       # bridge overview (with submodule index)
    README_chronometer.md           # library submodule doc (README_<topic>.md #2)
    README_main.md                  # service submodule doc (README_<topic>.md #3)
    pyproject.toml
    src/tak_bridge_chronometer/
      __init__.py
      chronometer.py                # library – port of ArduIllusion.cpp:161 Chrono (GPIO UART)
      __main__.py                   # async hourly sync (local + UTC) – sync_once @52
    tests/
      test_chronometer.py
    systemd/
      tak-bridge-chronometer.service  # GPIO UART /dev/serial0
  tak-bridge-escpos/
    README.md                       # bridge overview (index of 2 submodules)
    README_escpos.md                # driver deep dive (softserial BCM tx_pin/rx_pin + baudrate, ESC/POS fat)
    README_main.md                  # service deep dive (PrintRequest→printer, Cot 911→print_alarm)
    pyproject.toml                  # deps pyserial/pigpio/takpi-common/takstream + softserial GPIO
    src/tak_bridge_escpos/
      __init__.py                   # re-exports EscPosPrinter, PrintRequest
      escpos.py                     # driver (FakeSerial/SoftSerial/HardwareSerial, fat/print_alarm, handle_print_request)
      __main__.py                   # service (EventBus PrintRequest/CotReceived→print, softserial vs port, health)
    tests/
      test_escpos.py                # 9 tests (FakeSerial, fat, alarm, PrintRequest via bus, Cot→print)
    systemd/
      tak-bridge-escpos.service     # softserial pigpiod dep, /tmp/tak-escpos-healthy
```

## Conventions

- Python 3.12+, Poetry, `black`/`mypy --strict`/`pylint`, `pytest`+`pytest-asyncio`
- Health files: `/tmp/tak-<bridge>-healthy`
- Env via `python-dotenv`, `.env` per bridge (not committed)
- See `AGENTS.md` for full standards.
