# takpi-common

Shared framework for takpi: **MCP23017 I2C daisy-chain (buttons/encoders/LEDs), EventBus, CotStream (takstream) integration, health/config**.

> **Not pytak** – CoT streaming via `takpi/python-tak-cot-streaming` submodule (`takstream.CotStream`/`CotEvent`), not `pytak`. See `README_cot.md:1`.

This directory is the **shared `takpi_common` library** (`src/takpi_common/`, Poetry project `takpi-common`, `mypy --strict` clean). Every hardware bridge `tak-bridge-*` depends on it via path dependency; it is never deployed alone but imported.

## Submodules – `README_<topic>.md` per Submodule

| # | Topic | File | Covers |
|---|-------|------|--------|
| 1 | **MCP23017 daisy-chain** | [`README_mcp23017.md`](README_mcp23017.md) | Driver `driver.py:1` (0x20-0x27, `FakeSMBus`), I2C wiring SDA GPIO2/SCL GPIO3, daisy-chain 8×16 GPIOs, `HardwareConfig` + `ButtonConfig`/`EncoderConfig`/`LedConfig`, `ButtonState` debounce, `EncoderState` quadrature, `HardwareManager` poll 5 ms (200 Hz) + `LedCommand`→`OLAT` + blink, `i2cdetect` troubleshooting |
| 2 | **EventBus** | [`README_bus.md`](README_bus.md) | Central `bus.py:1` async pub/sub: typed `ButtonEvent`/`EncoderEvent`/`LedCommand`/`CotReceived`/`CotSend`, `subscribe`/`publish`/`wait_for`, wildcard `object`, bidirectional hardware→main→CoT and CoT→main→hardware, `HardwareManager` + `CotBus` wiring, `TakPiApp` handlers, tests |
| 3 | **CotStream (takstream)** | [`README_cot.md`](README_cot.md) | `cot_bus.py:1` `CotBus` bridging `python-tak-cot-streaming` (`takstream.CotStream`/`CotEvent`, git submodule `../python-tak-cot-streaming`, `develop=true`) ↔ `EventBus`: `CotReceived`/`CotSend`, `CotConfig` (host/port/certs/callsign), keepalive `t-x-c-t`, TLS via `cert`/`key`, submodule setup, path dep, `TakPiApp` button→CoT / CoT→LED, live tests |
| 4 | **TakPiApp main** | [`README_app.md`](README_app.md) | Example main `app.py:1` (`TakPiApp`, `CotConfig`): owns `EventBus` + `HardwareManager` + `CotBus`, subscribes `ButtonEvent`/`EncoderEvent`/`CotReceived`, example handlers `_on_button` (emergency → `CotSend` + `LedCommand`), `_on_encoder` → LED, `_on_cot` (emergency blink, chat→LED, team Cyan), startup `start()`/`stop()`, `.env` `TAK_HOST`, fake and real demos |

All are thorough (300+ lines each) and linked from top-level `../README.md:15` (index of 8 `README_<topic>.md`). Also see upstream docs in `../python-tak-cot-streaming/README.md` for takstream.

## Quick Start (Shared Framework)

```bash
# Clone with submodule (takstream, not pytak)
git clone --recurse-submodules <takpi>
# or if already cloned:
git submodule update --init --recursive  # fetches python-tak-cot-streaming

# Install common (includes takstream via path develop)
poetry install --directory common
poetry run --directory common pytest -v  # 23 tests with FakeSMBus/AsyncMock, no hardware, no TAK
poetry run --directory common black --check src tests && poetry run --directory common mypy src && poetry run --directory common pylint src

# Demo: fake hardware + fake CoT (no Pi, no TAK server)
poetry run --directory common python examples/panel_demo.py
# With real TAK server (needs .env with TAK_HOST/PORT/CERT)
poetry run --directory common python examples/panel_demo.py --with-cot
```

## Layout (common)

```
common/
  README.md                # this file (index)
  README_mcp23017.md       # MCP23017 daisy-chain (driver/io/manager, 5 ms poll)
  README_bus.md            # EventBus bidirectional (hardware ↔ main ↔ CoT)
  README_cot.md            # CotStream via CotBus (takstream, not pytak)
  README_app.md            # TakPiApp main wiring (button→CoT, CoT→LED)
  pyproject.toml           # takpi-common + path dep python-tak-cot-streaming develop
  src/takpi_common/
    __init__.py
    bus.py                 # EventBus (pub/sub, wait_for, wildcard)
    config.py              # dotenv helpers
    health.py              # report_health (shared)
    app.py                 # TakPiApp + CotConfig (example main)
    cot_bus.py             # CotBus (CotReceived/CotSend bridge to takstream)
    mcp23017/
      driver.py            # MCP23017 (FakeSMBus, 0x20-0x27, IODIR/GPPU/GPIO/OLAT)
      io.py                # Button/Encoder/Led configs + ButtonEvent/EncoderEvent/LedCommand
      manager.py           # HardwareManager (daisy-chain, debounce, quadrature, blink)
  examples/
    panel_demo.py          # fake hardware + fake CoT demo (no Pi, no TAK)
  tests/
    test_bus.py / test_driver.py / test_manager.py / test_cot_bus.py / test_app.py
```

## Conventions (per AGENTS.md)

- **Python 3.12+**, Poetry `poetry-core>=2.0`, `black` 88 cols `py312`, `mypy --strict`, `pylint`, `pytest`+`pytest-asyncio`+`FakeSMBus` (no hardware)
- **Takstream, not pytak:** `takstream` via submodule `../python-tak-cot-streaming`, `develop=true` path dep, `CotBus` ↔ `EventBus`
- **Daisy-chain:** Multiple MCP23017 at 0x20-0x27 share `SDA`/`SCL` + distinct A0-A2 straps; one `smbus` lock, 5 ms poll
- **EventBus:** Typed `publish`/`subscribe`, wildcard `object`, `wait_for` for tests

## References

- `AGENTS.md:1` (architecture, takstream, MCP23017, EventBus)
- `../README.md:15` (top-level index of 8 `README_<topic>.md`)
- `../README_chronometer.md:1` (chronometer GPIO UART contrast, not I2C)
- `../python-tak-cot-streaming/README.md:1` (upstream takstream)
- `src/takpi_common/bus.py:1`, `src/takpi_common/mcp23017/driver.py:1`, `src/takpi_common/cot_bus.py:1`, `src/takpi_common/app.py:1`
