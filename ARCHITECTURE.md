# takpi – Architecture Overview

> **Monorepo:** `takpi/` – Raspberry Pi TAK Hardware Bridge Collection  
> **Python:** 3.12+, Poetry `poetry-core>=2.0`, `black` 88 `py312`, `mypy --strict`, `pylint`, `pytest`+`Fake*` – all via `.venv`  
> **TAK:** `python-tak-cot-streaming` (`takstream.CotStream`/`CotEvent`, **not `pytak`**) via git submodule `takpi/python-tak-cot-streaming` (`../python-tak-cot-streaming`) + `python-takserver-api` (Mission API) – mTLS PEMs  
> **Platform:** Raspberry Pi OS, `systemd` per bridge, `HEALTH_FILE=/tmp/tak-<bridge>-healthy`, `.env` gitignored

This document aggregates the monorepo architecture. For per-submodule deep dives see `README.md:15` (11 `README_<topic>.md` index) and `AGENTS.md:1`.

---

## 1. System Context

```mermaid
flowchart TB
    subgraph Pi["Raspberry Pi – takpi host"]
        direction TB
        COMMON["common/src/takpi_common/<br/>bus.py:1 EventBus<br/>cot_bus.py:1 CotBus<br/>app.py:1 TakPiApp"]
        MCP["MCP23017 daisy-chain<br/>common/src/takpi_common/mcp23017/<br/>driver.py:1 0x20-0x27<br/>manager.py:1 5ms poll 200Hz"]
        CHRONO["tak-bridge-chronometer<br/>chronometer.py:1 GSA72<br/>/dev/serial0 GPIO14/15 @38400"]
        ESCPOS["tak-bridge-escpos<br/>escpos.py:1 EscPosPrinter<br/>softserial BCM tx/rx @9600-115200<br/>or port /dev/ttyUSB0"]
    end
    subgraph HW["Hardware (TTL 3.3V)"]
        BTN["Buttons / Encoders<br/>GPA0..GPB7"]
        LED["LEDs (OLAT)"]
        GSA["Flight Illusion GSA-072<br/>Davtron chronometer"]
        PRT["Thermal ESC/POS 58mm<br/>Epson TM-T20 / Xprinter"]
    end
    subgraph TAK["TAK Server"]
        COT["CoT streaming<br/>8087 plaintext / 8089 TLS<br/>CotStream keepalive ping t-x-c-t"]
        API["REST API 8443<br/>Mission / DataPackage<br/>python-takserver-api"]
    end
    ATAK["ATAK / TAK clients"]

    MCP ---|"I2C-1 SDA GPIO2/SCL GPIO3<br/>0x20 A0=GND → 0x21 A0=VCC"| BTN
    MCP --- LED
    CHRONO ---|"GPIO UART<br/>pin8 TXD → gauge RxD"| GSA
    ESCPOS ---|"softserial BCM27→RX<br/>pigpio wave_add_serial<br/>or /dev/serial0"| PRT
    COMMON <-->|"ButtonEvent/EncoderEvent<br/>LedCommand/PrintRequest"| MCP
    COMMON <-->|"CotSend/CotReceived"| COT
    COT --- ATAK
    COMMON -.-> API
```

**Not pytak:** every new bridge uses `takstream` – `CotEvent.marker`/`sa_report`/`chat_message`/`emergency_alert`, `CotStream.connect(host,port,cert,key,callsign,team,role)` `common/README_cot.md:1`, submodule `takpi/python-tak-cot-streaming/src/takstream/cot.py:1`/`stream.py:1`.

---

## 2. Monorepo Layout

```mermaid
flowchart LR
    subgraph ROOT["takpi/"]
        AGENTS["AGENTS.md:1<br/>coordination"]
        RMD["README.md:1<br/>11 README_<topic>.md index"]
        RCH["README_chronometer.md<br/>README_escpos.md"]
        SUB["python-tak-cot-streaming/<br/>git submodule ../python-tak-cot-streaming<br/>src/takstream/"]
        COMMON["common/<br/>takpi_common<br/>bus.py / cot_bus.py / app.py<br/>mcp23017/driver.py, io.py, manager.py<br/>5ms poll / FakeSMBus"]
        CHRONO_B["tak-bridge-chronometer/<br/>chronometer.py + __main__.py<br/>/dev/serial0"]
        ESCPOS_B["tak-bridge-escpos/<br/>escpos.py + __main__.py<br/>tx_pin/rx_pin + PrintRequest"]
    end
    COMMON -->|"path develop"| CHRONO_B
    COMMON --> ESCPOS_B
    SUB -->|"path develop"| COMMON
    SUB -->|"path develop"| ESCPOS_B
```

```
takpi/
  AGENTS.md, README.md + README_chronometer.md / README_escpos.md (top-level aggregations, 11 docs)
  python-tak-cot-streaming/  # git submodule ../python-tak-cot-streaming (takstream, not pytak)
  common/
    src/takpi_common/bus.py, cot_bus.py, app.py, health.py, config.py, mcp23017/driver.py, io.py, manager.py
    tests/test_*.py (23, FakeSMBus/AsyncMock)
    examples/panel_demo.py (fake hardware + fake CoT)
  tak-bridge-chronometer/  src/tak_bridge_chronometer/chronometer.py, __main__.py, tests, systemd, README_*.md
  tak-bridge-escpos/       src/tak_bridge_escpos/escpos.py, __main__.py, tests, systemd, README_*.md
  ARCHITECTURE.md          # this file
```

**Rules:** each `tak-bridge-*` is `poetry` independent (`pyproject.toml` `poetry-core>=2.0`), shared → `common/src/takpi_common` via `path = "../common"`, no cross-bridge imports except `takpi_common`.

---

## 3. EventBus – Bidirectional Decoupling

**Central** `common/src/takpi_common/bus.py:1` `EventBus` – typed `publish`/`subscribe`/`wait_for`, wildcard `object`, `async`/`sync` handlers, exception-isolated.

```mermaid
flowchart LR
    subgraph HW_MGR["HardwareManager<br/>manager.py:1"]
        BTN_Evt["ButtonEvent<br/>io.py:15"]
        ENC_Evt["EncoderEvent<br/>io.py:27"]
        LED_Cmd["LedCommand<br/>io.py:41"]
    end
    BUS["EventBus<br/>bus.py:1"]
    subgraph APP["TakPiApp<br/>app.py:1"]
        H1["on_button: btn_emergency → CotSend(emergency_alert) + LedCommand"]
        H2["on_encoder: delta→LedCommand"]
        H3["on_cot: CotReceived(b-a-o-tif)→LedCommand blink + PrintRequest 911"]
    end
    subgraph COT_MOD["CotBus<br/>cot_bus.py:1"]
        COT_R["CotReceived<br/>cot: CotEvent"]
        COT_S["CotSend<br/>cot: CotEvent"]
    end
    subgraph STREAM["CotStream<br/>python-tak-cot-streaming/src/takstream/stream.py:1"]
        SND["send()"]
        RCV["async for event in stream"]
    end
    subgraph PRT["EscPosPrinter<br/>tak-bridge-escpos/src/tak_bridge_escpos/escpos.py:1"]
        PR_REQ["PrintRequest<br/>text/raw_bytes/fat/align/cut_after"]
        PR_ALM["print_alarm(callsign, lat, lon, location)<br/>fat ***911 ALARM*** + cut"]
    end

    HW_MGR -- "publish ButtonEvent/EncoderEvent<br/>_poll_once 5ms" --> BUS
    BUS -- "subscribe" --> H1
    BUS -- "subscribe" --> H2
    H1 -- "publish CotSend" --> BUS
    H1 -- "publish LedCommand" --> BUS
    BUS -- "subscribe CotSend" --> COT_MOD
    COT_MOD -- "stream.send + parent link" --> SND --> STREAM
    STREAM -- "receive → CotReceived" --> COT_MOD -- "publish" --> BUS
    BUS -- "subscribe CotReceived" --> H3
    H3 -- "publish LedCommand blink 500ms<br/>team Cyan → led_cyan" --> BUS
    H3 -- "publish PrintRequest OR direct print_alarm" --> BUS
    BUS -- "subscribe LedCommand" --> HW_MGR
    BUS -- "subscribe PrintRequest" --> PRT
    PRT -- "handle_print_request → _write GS!/ESC E" --> PRT
    COT_MOD -. "also" .-> BUS
```

**Hardware → Main → CoT** (MCP button triggers emergency):
```mermaid
sequenceDiagram
    participant MCP as MCP23017 0x20
    participant MGR as HardwareManager
    participant BUS as EventBus
    participant APP as TakPiApp._on_button
    participant COT as CotBus/CotStream
    participant PRT as EscPosPrinter
    MCP->>MGR: read_gpio() GPA0 low (active-low, debounce 50ms)
    MGR->>BUS: publish ButtonEvent(id=btn_emergency, pressed=true)
    BUS->>APP: on_button
    APP->>BUS: publish CotSend(CotEvent.emergency_alert)
    APP->>BUS: publish LedCommand(id=led_alert, state=true)
    BUS->>COT: CotSend → stream.send()
    BUS->>MGR: LedCommand → write_pin OLAT → LED on
    Note over PRT: optional visual ack via PrintRequest
```

**CoT → Main → LED + Paper** (emergency → blink + 911 print):
```mermaid
sequenceDiagram
    participant STREAM as CotStream
    participant COT as CotBus
    participant BUS as EventBus
    participant APP as TakPiApp._on_cot
    participant MGR as HardwareManager
    participant PRT as EscPosPrinter
    STREAM->>COT: CotEvent b-a-o-tif (is_emergency)
    COT->>BUS: publish CotReceived(cot)
    BUS->>APP: on_cot
    APP->>BUS: publish LedCommand(id=led_alert, blink_ms=500)
    APP->>BUS: publish PrintRequest OR call printer.print_alarm
    BUS->>MGR: LedCommand → _blink_led (toggle OLAT 0.25s)
    BUS->>PRT: PrintRequest / print_alarm
    PRT->>PRT: fat("*** 911 ALARM ***") + From/Loc/Time/Type/Remarks + cut
```

---

## 4. Hardware – Daisy-Chain vs Point-to-Point

```mermaid
flowchart TB
    subgraph PI["Pi 40-pin"]
        P1["Pin1 3.3V"]
        SDA["Pin3 GPIO2 SDA"]
        SCL["Pin5 GPIO3 SCL"]
        GNDP["Pin6 GND"]
        TXH["Pin8 GPIO14 TXD<br/>/dev/serial0"]
        RXH["Pin10 GPIO15 RXD"]
        TXS["Pin13 BCM27 softserial TX<br/>pigpio wave"]
        RXS["Pin15 BCM22 softserial RX opt"]
    end
    subgraph I2C["I2C-1 MCP23017 chain 0x20-0x27"]
        M0["MCP0 0x20<br/>A0=GND GPA0 btn<br/>GPB0/1 enc A/B<br/>GPA7 LED"]
        M1["MCP1 0x21<br/>A0=VCC GPA0 LED"]
        M2["MCP2 0x27<br/>A0-2=VCC"]
    end
    subgraph UART["GPIO UART"]
        CH["GSA-072 chronometer<br/>Pin10 RxD"]
    end
    subgraph SOFT["Softserial"]
        PR["ESC/POS RX<br/>TTL 5V"]
    end
    SDA --- M0 & M1 & M2
    SCL --- M0 & M1 & M2
    P1 --- M0 & M1 & M2
    GNDP --- M0 & M1 & M2
    M0 ---|"shared SDA/SCL<br/>distinct A0-A2<br/>8×16=128 GPIOs"| M1
    TXH --> CH
    TXS --> PR
```

* Chronometer: `tak-bridge-chronometer/src/tak_bridge_chronometer/chronometer.py:1` `GSA72_ID=109`, `encode_packet` `ArduIllusion.cpp:32` 2 ms/byte, service `__main__.py:52` `sync_once` hourly `local`+`UTC`.
* MCP: `common/src/takpi_common/mcp23017/driver.py:1` `FakeSMBus` for CI, `manager.py:1` 5 ms poll, debounce 50 ms, quadrature `ENCODER_TABLE`.
* ESC/POS: `tak-bridge-escpos/src/tak_bridge_escpos/escpos.py:1` `HardwareSerial` (`pyserial`) vs `SoftSerial` (`pigpio.wave_add_serial`, `FakePigpio`), configurable `ESCPOS_TX_PIN`/`ESCPOS_RX_PIN` BCM + `ESCPOS_BAUDRATE` 9600-115200, styles `fat` `GS ! 0x11`/`ESC E`, `print_alarm` `PrintRequest` `escpos.py:42`.

---

## 5. Runtime – systemd + Health + Config

```mermaid
flowchart TB
    ENV[".env per bridge (gitignored)<br/>TAK_HOST, TAK_PORT, CLIENT_CERT/KEY,<br/>ESCPOS_TX_PIN/BAUDRATE, CHRONO_PORT"]
    POETRY["Poetry .venv per bridge<br/>poetry install --directory common|tak-bridge-*"]
    SUB["git submodule<br/>python-tak-cot-streaming<br/>../python-tak-cot-streaming"]
    SVC1["systemd<br/>tak-bridge-chronometer.service<br/>User pi Group dialout<br/>/dev/serial0 @38400<br/>ExecStartPre rm /tmp/tak-chronometer-healthy"]
    SVC2["systemd<br/>tak-bridge-escpos.service<br/>After pigpiod.service<br/>ESCPOS_TX_PIN=27<br/>/tmp/tak-escpos-healthy"]
    SVC3["systemd<br/>tak-bridge-mcp / TakPiApp<br/>I2C-1 0x20-0x27<br/>poll 5ms<br/>/tmp/tak-mcp-healthy"]
    POETRY --> SVC1 & SVC2 & SVC3
    SUB --> POETRY
    ENV --> SVC1 & SVC2 & SVC3
```

* Health: `common/src/takpi_common/health.py:1` `report_health(file, ok)` – absent = healthy, `HEALTH_MAX_ERRORS=3`, `ExecStartPre rm`, `journalctl -u tak-bridge-* -f`, `cat /tmp/tak-*-healthy`.
* Config: `python-dotenv`, `common/src/takpi_common/config.py:1` `load_env`, `TAK_*`, `ESCPOS_*`, `CHRONO_PORT`, `I2C_BUS`.
* Security: `pip-audit` pre-commit, never commit `certs/*.pem/*.p12`, `.gitignore` + `detect-secrets`.
* Testing: `poetry run --directory common pytest -v` 23 tests `FakeSMBus`/`FakeSerial`/`AsyncMock`; `tak-bridge-chronometer` 7, `tak-bridge-escpos` 9 – no Pi/TAK needed, `poetry run mypy src && pylint src` 10.00.

---

## 6. Docs Index (11 `README_<topic>.md`)

`README.md:15` lists all, `AGENTS.md:1` coordination:

* Top-level `README_chronometer.md`, `README_escpos.md`
* `common/README_mcp23017.md`, `README_bus.md`, `README_cot.md`, `README_app.md`
* `tak-bridge-chronometer/README_chronometer.md` + `README_main.md`
* `tak-bridge-escpos/README_escpos.md` + `README_main.md`
* `python-tak-cot-streaming/README.md` upstream

Ready for wiring – `common/examples/panel_demo.py` runs fake hardware+fake CoT (`FakeSMBus` + `AsyncMock` stream) without Pi/TAK to validate `ButtonEvent → CotSend` and `CotReceived → LedCommand`/`PrintRequest`.

