# README_app – TakPiApp Main Process (Hardware ↔ CoT Wiring)

> **Path:** `takpi/common/src/takpi_common/app.py:1` (`TakPiApp`, `CotConfig`)  
> **Bus:** `takpi/common/src/takpi_common/bus.py:1` (`EventBus`)  
> **Hardware:** `takpi/common/src/takpi_common/mcp23017/manager.py:1` (`HardwareManager`, `HardwareConfig`) + `takpi/common/src/takpi_common/mcp23017/io.py:1`  
> **CoT:** `takpi/common/src/takpi_common/cot_bus.py:1` (`CotBus`, `CotReceived`/`CotSend`) + `takpi/python-tak-cot-streaming` (`takstream.CotStream`/`CotEvent`)

This is the thorough reference for the **example main takpi process** that wires the bidirectional MCP23017 hardware and CoT streaming together via the central `EventBus`. It satisfies the requirement that buttons/encoders trigger actions in main and main controls LEDs based on CoTs.

---

## 1. Purpose – The "Main" Process

Takpi is **not a single project** (per `AGENTS.md:5`), but many bridges share one Pi. The MCP23017 framework introduces a **central main process** that:

- Owns one `EventBus` (shared hub)
- Runs `HardwareManager` for the daisy-chained MCP23017 chain (polling + debounce + quadrature)
- Optionally connects `CotStream` (takstream) via `CotBus`
- Registers business logic: `ButtonEvent` → `CotSend` (send emergency), `CotReceived` → `LedCommand` (blink alert), `EncoderEvent` → LED/position, etc.

```
            ┌─────────────┐
            │  TakPiApp   │  common/src/takpi_common/app.py:1
            │  (main)     │  bus = EventBus()
            └──────┬──────┘
      ┌───────────┼────────────┐
      │           │            │
 Hardware     CotBus      Example handlers
 Manager   (takstream)    _on_button/_on_cot/_on_encoder
   │           │            │
   ├──ButtonEvent           ├──CotReceived ──► LedCommand (blink)
   ├──EncoderEvent          └──CotSend ◄── Button
   └──LedCommand (subscribe)
```

- **Button/encoder → main:** HardwareManager publishes `ButtonEvent`/`EncoderEvent` on bus; main's `_on_button`/`_on_encoder` subscribed on bus can publish `CotSend` (→ CotStream) or `LedCommand`.
- **Main → LED:** Main's `_on_cot` subscribed to `CotReceived` (from CotBus) publishes `LedCommand`; HardwareManager subscribed to `LedCommand` drives MCP23017 `OLAT` → LED.

All without direct coupling: hardware and CoT know only the bus.

---

## 2. Architecture

### 2.1 Class `TakPiApp` (`app.py:43`)

| Member | Type | Description |
|--------|------|-------------|
| `bus` | `EventBus` | Shared hub (injected or created) |
| `hardware_cfg` | `HardwareConfig` | Declarative MCP wiring (devices, buttons, encoders, leds, poll interval, i2c_bus, smbus) |
| `cot_cfg` | `CotConfig | None` | TakStream connection (host/port/certs/callsign/team/role); `None` → no CoT, hardware only |
| `hardware` | `HardwareManager | None` | Created in `start()` |
| `cot_bus` | `CotBus | None` | Created after `CotStream.connect` |
| `_stream` | `CotStream | None` | Raw takstream stream |
| `_subs` | `list[unsubscribe]` | Handlers registered for cleanup |

| Method | Description |
|--------|-------------|
| `__init__(hardware_cfg, cot_cfg=None, bus=None)` | Store configs, no I/O |
| `async start()` | `HardwareManager.start()` → subscribe `ButtonEvent`/`EncoderEvent` → if `cot_cfg` connect `CotStream` → `CotBus.start()` → subscribe `CotReceived` |
| `async stop()` | Unsubscribe, `CotBus.stop()`, `stream.close()`, `HardwareManager.stop()` |
| `async __aenter__/__aexit__` | Context manager |
| `_connect_cot(cfg)` | Lazy `from takstream import CotStream`, `await CotStream.connect(...)`, create `CotBus` |
| `_on_button(evt)` | Example: `btn_emergency` pressed → `CotEvent.emergency_alert` via `CotSend` + `LedCommand`; other buttons `btn→led` toggle |
| `_on_encoder(evt)` | Example: `delta>0` → `LedCommand` on |
| `_on_cot(evt)` | Example: `is_emergency()` → `LedCommand blink 500ms`, `is_chat()` → `led_chat`, `team=="Cyan"` → `led_cyan` |

All handlers are `async` and use `await bus.publish(...)`.

### 2.2 `CotConfig` (`app.py:29`)

| Field | Description |
|-------|-------------|
| `host`, `port` | TAK server streaming endpoint (e.g., `tak.example.com`, 8089 TLS, 8087 plaintext) |
| `cert`, `key`, `ca` | PEM paths for mTLS (both cert+key required together); `None` → cleartext |
| `callsign`, `team`, `role` | Identity for initial SA; all three required together for server routing |
| `cot_type`, `uid` | Own SA type/uid (auto UUID4) |

Secrets from `.env` via `takpi_common.config.load_env()` – never committed.

### 2.3 Startup Sequence (Async)

```
TakPiApp.start()
  ├─ HardwareManager(cfg,bus).start()
  │    ├─ for each device: setup() (IODIR/GPPU), configure pins
  │    ├─ bus.subscribe(LedCommand, _handle_led_command)
  │    └─ create_task(_poll_loop)  // 5 ms poll
  ├─ bus.subscribe(ButtonEvent, _on_button)
  ├─ bus.subscribe(EncoderEvent, _on_encoder)
  └─ if cot_cfg:
       ├─ CotStream.connect(...)  // TLS context, identity SA, background tasks
       ├─ CotBus(bus, stream).start()  // subscribe CotSend, recv_loop
       └─ bus.subscribe(CotReceived, _on_cot)
```

Shutdown reverses: unsubscribe, `CotBus.stop()`, `stream.close()` (sends `t-x-d-d`), `HardwareManager.stop()` (cancel poll/blink, close I2C).

---

## 3. Configuration

### 3.1 Hardware (`HardwareConfig`, `manager.py:48`)

See `README_mcp23017.md:4` – same declarative config:

```python
HardwareConfig(
    i2c_bus=1,
    devices=[0x20, 0x21],
    buttons=[ButtonConfig(id="btn_emergency", device_addr=0x20, pin=0)],
    encoders=[EncoderConfig(id="enc_freq", device_addr=0x20, pin_a=1, pin_b=2, pin_button=3)],
    leds=[LedConfig(id="led_alert", device_addr=0x21, pin=0), LedConfig(id="led_chat", device_addr=0x21, pin=1)],
    poll_interval_ms=5,
    smbus=FakeSMBus(),  # None for real hardware
)
```

### 3.2 CoT (`CotConfig`, `app.py:29`)

From `.env` (gitignored) or direct:

```python
CotConfig(
    host=os.getenv("TAK_HOST", "tak.example.com"),
    port=int(os.getenv("TAK_PORT", "8089")),
    cert=os.getenv("TAK_CERT"),  # certs/client.pem or None
    key=os.getenv("TAK_KEY"),
    ca=os.getenv("TAK_CA"),
    callsign="PI-PANEL",
    team="Cyan",
    role="Team Member",
)
```

No `pytak` – only `takstream` via submodule `python-tak-cot-streaming`.

---

## 4. Bidirectional Examples (Core Requirement)

### 4.1 Button → CoT (Hardware → Main → CoT)

`_on_button` (`app.py:143`) – button `btn_emergency` pressed → publish `CotSend` with `CotEvent.emergency_alert`, plus LED ack:

```python
async def _on_button(self, evt: ButtonEvent):
    if evt.id == "btn_emergency" and evt.pressed:
        cot = CotEvent.emergency_alert(lat=60.0, lon=24.0, callsign="PI-PANEL")
        await self.bus.publish(CotSend(cot=cot))  # CotBus → CotStream.send
        await self.bus.publish(LedCommand(id="led_alert", device_addr=0x20, pin=0, state=True))
```

Generic `btn→led` toggle for other buttons: `led_id = evt.id.replace("btn","led")`.

### 4.2 Encoder → LED / CoT

`_on_encoder` (`app.py:162`):

```python
async def _on_encoder(self, evt: EncoderEvent):
    led_state = evt.delta > 0  # CW → on, CCW → off
    await self.bus.publish(LedCommand(id=f"led_{evt.id}", device_addr=evt.device_addr, pin=0, state=led_state))
```

Could also `await self.cot_bus.stream.set_position(...)` for map panning.

### 4.3 CoT → LED (Main → Hardware, e.g., Emergency Blinks)

`_on_cot` (`app.py:169`) – `CotReceived` → `LedCommand`:

```python
async def _on_cot(self, evt: CotReceived):
    cot = evt.cot
    if cot.is_emergency():
        await self.bus.publish(LedCommand(id="led_alert", device_addr=0x20, pin=0, state=True, blink_ms=500))
    if cot.is_chat():
        await self.bus.publish(LedCommand(id="led_chat", device_addr=0x20, pin=1, state=True))
    if getattr(cot, "team", None) == "Cyan":
        await self.bus.publish(LedCommand(id="led_cyan", device_addr=0x21, pin=0, state=True))
```

This satisfies *"main takpi process should be able to control LEDs based on whatever happens somewhere in the application, e.g., specific CoTs"*.

### 4.4 Custom Logic – Replace Handlers

`TakPiApp` handlers are examples – for production, subclass or replace:

```python
app = TakPiApp(hw_cfg, cot_cfg)
await app.start()
# Override: unsubscribe example and subscribe custom
for unsub in app._subs: unsub()
app.bus.subscribe(ButtonEvent, my_custom_button_handler)
app.bus.subscribe(CotReceived, my_custom_cot_handler)
```

Or copy `app.py` as template for your own `main.py` – the pattern stays: one `EventBus`, one `HardwareManager`, one `CotBus`.

---

## 5. Running

### 5.1 Prerequisites (Pi)

```bash
sudo raspi-config → I2C → Yes; reboot
sudo usermod -a -G i2c $USER; newgrp i2c
i2cdetect -y 1  # see 20,21
sudo usermod -a -G dialout $USER  # for /dev/serial0 if also using chronometer
poetry install --directory common  # installs smbus2, takstream via path develop
```

### 5.2 Standalone Demo (No TAK Server, Fake Hardware)

```bash
poetry run --directory common python -c "
import asyncio
from takpi_common.bus import EventBus
from takpi_common.mcp23017.driver import FakeSMBus
from takpi_common.mcp23017.manager import HardwareConfig
from takpi_common.mcp23017.io import ButtonConfig, LedConfig
from takpi_common.app import TakPiApp

async def demo():
    bus = EventBus()
    fake = FakeSMBus()
    hw = HardwareConfig(
        devices=[0x20],
        buttons=[ButtonConfig(id='btn1', device_addr=0x20, pin=0)],
        leds=[LedConfig(id='led1', device_addr=0x20, pin=8)],
        smbus=fake,
    )
    async with TakPiApp(hardware_cfg=hw, bus=bus):
        # Simulate press: drive pin low
        fake.set_input(0x20, 0, False)
        await asyncio.sleep(0.1)
        print('led state after press:', fake.get_olat(0x20, 8))
        fake.set_input(0x20, 0, True)
        await asyncio.sleep(0.1)
        print('led state after release:', fake.get_olat(0x20, 8))

import asyncio; asyncio.run(demo())
"
```

### 5.3 With Real Hardware + TAK Server

Create `.env` (gitignored) in `takpi/` or `takpi/common/`:

```
TAK_HOST=tak.example.com
TAK_PORT=8089
TAK_CERT=certs/client.pem
TAK_KEY=certs/client.key
TAK_CALLSIGN=PI-PANEL
TAK_TEAM=Cyan
TAK_ROLE=Team Member
```

Run main process (example):

```bash
# Create your main.py copying app.py pattern
poetry run --directory common python your_main.py  # your_main uses TakPiApp with real i2c_bus=1
```

Check logs: `Button btn_emergency pressed → CotSend emergency_alert → CotReceived → led_alert blink`.

---

## 6. Wiring Recap (for App Context)

See `README_mcp23017.md:2` for full daisy-chain wiring. For App demo with 2 boards:

- Board 0x20: inputs (buttons/encoders) – GPA0 button, GPB0-2 encoder
- Board 0x21: outputs (LEDs) – GPA0 alert, GPA1 chat, etc.
- Shared SDA (GPIO2 pin3) + SCL (GPIO3 pin5) + GND + 3.3V, addresses via A0-A2 straps, 4.7k pull-ups.

No USB; all via I2C `1`.

---

## 7. Testing (No Hardware, No Server)

| Test | Coverage |
|------|----------|
| `tests/test_bus.py` | EventBus publish/subscribe, wildcard, wait_for, exception isolation |
| `tests/test_driver.py` | FakeSMBus + MCP23017 R/W |
| `tests/test_manager.py` | HardwareManager poll → ButtonEvent/EncoderEvent, LedCommand → OLAT, blink |
| `tests/test_cot_bus.py` | Fake CotStream → CotReceived, CotSend → stream.send |
| `tests/test_app.py` | TakPiApp with FakeSMBus + fake stream: button → CotSend, Cot → LedCommand |

Run:

```bash
poetry run --directory common pytest -v
poetry run --directory common black --check src tests
poetry run --directory common mypy src
poetry run --directory common pylint src
```

All use `FakeSMBus`/`AsyncMock` – no Pi, no TAK server.

---

## 8. Troubleshooting (App Layer)

| Symptom | Cause | Fix |
|---------|-------|-----|
| Button press not triggering CoT | `_on_button` not subscribed or not pressed edge (debounce) | Check `bus.subscribed(ButtonEvent)`; `FakeSMBus.set_input` must be low for `>50 ms`; see `README_mcp23017.md:8` |
| LED not blinking on emergency CoT | `LedCommand` id mismatch or `HardwareManager` not started | Verify `LedConfig.id == LedCommand.id` and `device_addr`/`pin`; check `bus.subscribed(LedCommand)` after `app.start()` |
| `takstream not available` warning | Submodule not cloned | `git submodule update --init --recursive`; `poetry install --directory common` |
| `CotStream.connect` fails `FileNotFoundError cert` | Bad `TAK_CERT` path | Use absolute `certs/client.pem` or `TAK_CERT` from `.env`; certs gitignored |
| No CoT→LED | `CotReceived` not published (no identity SA → server not routing) | Provide `callsign`+`team`+`role` in `CotConfig`; check `stream.stats` |
| Encoder missed | Poll interval too slow | Reduce `poll_interval_ms` 5→2 ms; check I2C bus not saturated |

---

## 9. References

- Bus: `takpi_common/bus.py:1` (`EventBus`)
- MCP framework: `takpi_common/mcp23017/driver.py:1` (`MCP23017`, `FakeSMBus`), `io.py:1` (`ButtonEvent` etc.), `manager.py:1` (`HardwareManager`, `HardwareConfig`) – see `README_mcp23017.md:1`
- CoT bridge: `takpi_common/cot_bus.py:1` (`CotBus`, `CotReceived`/`CotSend`) – see `README_cot.md:1`
- Their docs: `README_bus.md:1` (bus flow), `README_mcp23017.md:1`, `README_cot.md:1`
- Submodule: `takpi/python-tak-cot-streaming` (`../python-tak-cot-streaming`, `takstream.CotStream`/`CotEvent`) – `python-tak-cot-streaming/README.md:1`, `src/takstream/stream.py:1`, `src/takstream/cot.py:1`
- Chronometer (GPIO UART contrast): `tak-bridge-chronometer/README_chronometer.md:1` (GPIO UART, not I2C)
- Tests: `common/tests/test_app.py`, `common/tests/test_cot_bus.py`

---

## 10. Changelog

| Version | Date | Notes |
|---------|------|-------|
| 0.1.0 | 2026-09-07 | Initial TakPiApp – `EventBus` + `HardwareManager` (MCP23017 chain) + `CotBus` (takstream) bidirectional wiring, example handlers button→CoT→LED and CoT→LED, `mypy --strict` clean |

