# README_bus – EventBus (Main ↔ Hardware ↔ CoT)

> **Path:** `takpi/common/src/takpi_common/bus.py:1` (`EventBus`)  
> **IO events:** `takpi/common/src/takpi_common/mcp23017/io.py:1` (`ButtonEvent`, `EncoderEvent`, `LedCommand`)  
> **CoT events:** `takpi/common/src/takpi_common/cot_bus.py:1` (`CotReceived`, `CotSend`)  
> **Manager:** `takpi/common/src/takpi_common/mcp23017/manager.py:1` (`HardwareManager` publishes/subscribes)  
> **App:** `takpi/common/src/takpi_common/app.py:1` (`TakPiApp` example wiring)

This is the thorough reference for the **central EventBus** that decouples hardware (buttons/encoders → LEDs via MCP23017) from application logic (CoT streaming via `takstream`) in takpi's main process.

---

## 1. Purpose & Flow

**Problem:** Without a bus, hardware polling and CoT streaming would be tightly coupled: button handler would need direct references to `HardwareManager` and `CotStream`, LED logic would be scattered. The bus provides a single, typed, async pub/sub hub for bidirectional communication:

```
HardwareManager (MCP23017 poll) ──ButtonEvent/EncoderEvent──► EventBus ──► main handlers ──CotSend──► CotBus ──► CotStream (takstream, not pytak)
     ▲                                               │
     │                                          CotReceived
     │                                               ▼
     └────────── LedCommand ◄── main handlers ◄── EventBus ◄── CotBus ◄── CotStream
                  │
                  └──► HardwareManager ──► MCP23017 OLAT ──► LED
```

**Requirements satisfied:**

- Buttons/encoders **trigger actions in main takpi process** – manager publishes `ButtonEvent`/`EncoderEvent` on bus; main subscribes (`bus.subscribe(ButtonEvent, handler)`) and can send CoTs, control other LEDs, update state.
- Main process **controls LEDs based on whatever happens** (specific CoTs, etc.) – main subscribes to `CotReceived` and publishes `LedCommand`; manager subscribes to `LedCommand` and drives MCP23017.

**No pytak:** All CoT streaming uses `takpi/python-tak-cot-streaming` (submodule `../python-tak-cot-streaming`) via `takstream.CotStream`/`CotEvent`, bridged through `CotBus`. See `README_cot.md:1`.

---

## 2. Design

### 2.1 Pub/Sub Model

- **Typed topics:** `event_type` is the Python `type` (e.g., `ButtonEvent`). `publish(event)` dispatches to subscribers of `type(event)` **and** `object` wildcard.
- **Handlers:** `Callable[[T], Any | Awaitable[Any]]` – sync or `async def`. If handler returns awaitable, bus awaits it.
- **Error isolation:** One handler exception never breaks others (logged via `logger.exception`).
- **Unsubscribe:** `subscribe()` returns `unsubscribe()` callable – manager uses for `LedCommand`, app uses for lifecycle.
- **No queue backlog:** Bus is fire-and-forget; slow handlers should `create_task` for long work. For reliable delivery, handler should ack via `bus.publish(LedState)` etc.

### 2.2 Why Not Queue?

Alternative `asyncio.Queue` per subscriber would require consumer tasks; pub/sub with direct `await handler(event)` is simpler for takpi's low-rate events (<200 Hz poll, CoT <10 Hz). If needed, handler can debounce or buffer internally.

### 2.3 Thread Safety

`EventBus` is **asyncio-only** (not thread-safe). All `publish`/`subscribe` must be called from the event loop. `HardwareManager`'s I2C reads run in `run_in_executor` but `publish` is awaited back on the loop.

---

## 3. Public API (`bus.py:1`)

| Method | Signature | Description |
|--------|-----------|-------------|
| `__init__` | `()` | No args; holds `defaultdict(list)` of handlers + `asyncio.Lock` (unused currently, handlers list is not locked – add if concurrent subscribe needed) |
| `subscribe` | `(event_type: Type[T], handler: Callable[[T],...]) -> Callable[[],None]` | Register handler for `event_type`. Returns `unsubscribe()`. Use `object` for wildcard. |
| `subscribed` | `(event_type) -> int` | Count handlers (for tests/metrics) |
| `publish` | `(event: Any) -> Awaitable[None]` | Async – dispatches to exact type + wildcard, awaits each handler if needed, logs exceptions |
| `wait_for` | `(event_type: Type[T], timeout?) -> T` | Test helper – creates future, subscribes one-shot handler, waits via `asyncio.wait_for`, unsubscribes |

**Example:**

```python
bus = EventBus()

# Subscribe
unsub = bus.subscribe(ButtonEvent, lambda e: print(e.id, e.pressed))
# Wildcard – logs all events
bus.subscribe(object, lambda e: print("bus event", type(e).__name__))

# Publish (hardware → main)
await bus.publish(ButtonEvent(id="btn1", device_addr=0x20, pin=0, pressed=True))

# Wait for next button (test)
evt = await bus.wait_for(ButtonEvent, timeout=1.0)

# Unsubscribe
unsub()
```

---

## 4. Event Types

Bus is generic (`Any`), but takpi standardizes these typed events (see linked docs):

| Event | Module | Direction | Fields | Purpose |
|-------|--------|-----------|--------|---------|
| `ButtonEvent` | `mcp23017/io.py:15` | hardware → bus → main | `id, device_addr, pin, pressed, timestamp` + `released` | Button press/release after debounce |
| `EncoderEvent` | `io.py:27` | hardware → bus → main | `id, device_addr, delta (+1/-1), position, timestamp` | Quadrature turn per edge |
| `EncoderButtonEvent` | `io.py:34` | hardware → bus → main | `id, device_addr, pin, pressed, timestamp` | Encoder push-button |
| `LedCommand` | `io.py:41` | main → bus → hardware | `id, device_addr, pin, state, blink_ms?` | Main controls LED: `state=True` on, `blink_ms=500` blinks |
| `LedState` | `io.py:48` | hardware → bus (ack) | `id, device_addr, pin, state, timestamp` | Hardware confirms LED after `write_pin` |
| `CotReceived` | `cot_bus.py:15` | `CotBus` → bus → main | `cot: CotEvent, raw_xml?` | Inbound CoT from `takstream.CotStream` |
| `CotSend` | `cot_bus.py:22` | main → bus → `CotBus` → `CotStream` | `cot: CotEvent` | Outbound CoT request |

All are `@dataclass(frozen=True)` – hashable, comparable in tests.

---

## 5. Wiring – Bidirectional Examples

### 5.1 Hardware → Main → CoT (Button Triggers Emergency)

```python
from takpi_common.bus import EventBus
from takpi_common.mcp23017.io import ButtonEvent, LedCommand
from takpi_common.cot_bus import CotSend
from takstream import CotEvent  # from python-tak-cot-streaming submodule

bus = EventBus()

async def on_button(evt: ButtonEvent):
    if evt.id == "btn_emergency" and evt.pressed:
        # Send CoT
        cot = CotEvent.emergency_alert(lat=60.0, lon=24.0, callsign="PI-PANEL")
        await bus.publish(CotSend(cot=cot))
        # Visual ack
        await bus.publish(LedCommand(id="led_alert", device_addr=0x20, pin=0, state=True))

bus.subscribe(ButtonEvent, on_button)
# HardwareManager will publish ButtonEvent; CotBus will handle CotSend → CotStream.send
```

### 5.2 CoT → Main → LED (Emergency Blinks Alert)

```python
from takpi_common.cot_bus import CotReceived
from takpi_common.mcp23017.io import LedCommand

async def on_cot(evt: CotReceived):
    cot = evt.cot
    if hasattr(cot, "is_emergency") and cot.is_emergency():
        await bus.publish(LedCommand(id="led_alert", device_addr=0x20, pin=0, state=True, blink_ms=500))
    if getattr(cot, "team", None) == "Cyan":
        await bus.publish(LedCommand(id="led_cyan", device_addr=0x21, pin=0, state=True))

bus.subscribe(CotReceived, on_cot)
# CotBus publishes CotReceived for each inbound CotStream event
```

### 5.3 Encoder → Main → LED/CoT

```python
from takpi_common.mcp23017.io import EncoderEvent

async def on_encoder(evt: EncoderEvent):
    # Clockwise → LED on, CCW → off
    await bus.publish(LedCommand(id="led_enc", device_addr=0x21, pin=1, state=evt.delta>0))
    # Or: update takstream position
    # await cot_bus.stream.set_position(lat=..., lon=...)

bus.subscribe(EncoderEvent, on_encoder)
```

### 5.4 Main → Hardware Direct (Without Bus)

```python
# Also allowed, but bus is preferred for decoupling
await hardware_manager.set_led("led1", True)  # helper publishes LedCommand
```

---

## 6. Integration with Managers

### 6.1 HardwareManager

- **Publishes:** `ButtonEvent`, `EncoderEvent`, `EncoderButtonEvent` in `_poll_once` after debouncing/quadrature (see `manager.py:327`).
- **Subscribes:** `LedCommand` in `start()` via `bus.subscribe(LedCommand, _handle_led_command)` (`manager.py:170`). Handler writes to MCP23017 via `write_pin` in executor, then publishes `LedState` ack.

### 6.2 CotBus

- **Publishes:** `CotReceived` in `_recv_loop` for each `CotStream` event (`cot_bus.py:58`).
- **Subscribes:** `CotSend` in `start()` via `bus.subscribe(CotSend, _handle_send)` (`cot_bus.py:38`), which calls `stream.send(cot)`.

Both are started by `TakPiApp` (`app.py:70`).

---

## 7. Example – Full App Wiring (`app.py:1`)

`TakPiApp` shows the canonical main-process wiring:

```python
from takpi_common.bus import EventBus
from takpi_common.mcp23017.manager import HardwareConfig
from takpi_common.app import TakPiApp, CotConfig

bus = EventBus()
hw_cfg = HardwareConfig(
    devices=[0x20,0x21],
    buttons=[ButtonConfig(id="btn_emergency", device_addr=0x20, pin=0)],
    leds=[LedConfig(id="led_alert", device_addr=0x21, pin=0)],
)
cot_cfg = CotConfig(host="tak.example.com", port=8089, callsign="PI-PANEL", team="Cyan", role="Team Member")

app = TakPiApp(hardware_cfg=hw_cfg, cot_cfg=cot_cfg, bus=bus)
await app.start()  # starts HardwareManager + CotBus + example handlers
# Example handlers in app.py: _on_button → CotSend + LedCommand, _on_cot → LedCommand blink, etc.
```

Handlers in `app.py:143` are **examples** – replace with your domain logic while keeping bus as seam.

---

## 8. Testing (No Hardware)

Bus is pure asyncio, no I2C/takstream needed.

```python
bus = EventBus()
received = []

bus.subscribe(ButtonEvent, lambda e: received.append(e))
await bus.publish(ButtonEvent(id="btn1", device_addr=0x20, pin=0, pressed=True))
assert received[0].id == "btn1"

# Wait_for helper (test)
async def publisher():
    await asyncio.sleep(0.01)
    await bus.publish(EncoderEvent(id="enc1", device_addr=0x20, delta=1, position=1))

asyncio.create_task(publisher())
evt = await bus.wait_for(EncoderEvent, timeout=1.0)
assert evt.delta == 1

# Cot → LED test (mock stream)
from unittest.mock import AsyncMock
fake_stream = AsyncMock()
fake_stream.send = AsyncMock()
cot_bus = CotBus(bus, fake_stream)
await cot_bus.start()
await bus.publish(CotSend(cot=CotEvent.emergency_alert(lat=0, lon=0, callsign="T")))
assert fake_stream.send.called
```

Run:

```bash
poetry run --directory common pytest tests/test_bus.py -v
```

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ButtonEvent` never arrives | `HardwareManager` not started or poll interval too long | `await mgr.start()`; check `poll_interval_ms` 5 ms; verify `FakeSMBus.set_input` for tests or `i2cdetect` for hardware |
| `LedCommand` not lighting LED | No subscriber or wrong `id`/`addr`/`pin` | Manager subscribes only after `start()`; check `bus.subscribed(LedCommand)` >0; verify `LedConfig` ids match command; `write_pin` error logged |
| Handler not called | Wrong event type or not subscribed before publish | `bus.subscribe(CotReceived, ...)` must happen before `CotBus` starts publishing; check `type(event)` exactly matches subscription |
| Handler exception breaks bus | Exception in one handler | Bus logs exception and continues; check `logger.exception` in `bus.py:40` |
| `wait_for` timeout | Event never published | Ensure publisher task runs concurrently; use `asyncio.create_task` before `wait_for` |

---

## 10. References

- Bus: `takpi_common/bus.py:1`
- Hardware events: `takpi_common/mcp23017/io.py:1` (`ButtonEvent` etc.)
- Hardware manager: `takpi_common/mcp23017/manager.py:1`
- CoT bridge: `takpi_common/cot_bus.py:1` (`CotReceived`/`CotSend`, `CotBus`)
- App example: `takpi_common/app.py:1` (`TakPiApp`)
- Driver: `takpi_common/mcp23017/driver.py:1` (`FakeSMBus`)
- MCP docs: `README_mcp23017.md:1`
- TakStream submodule: `takpi/python-tak-cot-streaming` (`../python-tak-cot-streaming`, `takstream.CotStream`/`CotEvent`) – see `README_cot.md:1` and `python-tak-cot-streaming/README.md:1`
- Tests: `common/tests/test_bus.py`, `common/tests/test_cot_bus.py`

---

## 11. Changelog

| Version | Date | Notes |
|---------|------|-------|
| 0.1.0 | 2026-09-07 | Initial EventBus – typed pub/sub, wildcard `object`, `wait_for`, bidirectional hardware↔main↔CoT, `mypy --strict` clean |

