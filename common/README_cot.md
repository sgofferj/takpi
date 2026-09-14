# README_cot – CotStream Integration (takstream, not pytak)

> **Submodule:** `takpi/python-tak-cot-streaming` (git submodule `../python-tak-cot-streaming`, URL `https://github.com/sgofferj/python-tak-cot-streaming.git`)  
> **Python import:** `takstream` (`takstream.CotStream`, `takstream.CotEvent`, `takstream.Point`)  
> **Bus bridge:** `takpi/common/src/takpi_common/cot_bus.py:1` (`CotBus`, `CotReceived`, `CotSend`)  
> **Bus:** `takpi/common/src/takpi_common/bus.py:1` (`EventBus`)  
> **App wiring:** `takpi/common/src/takpi_common/app.py:1` (`TakPiApp`, `CotConfig`) – button→CoT and CoT→LED examples

This is the thorough reference for **CoT streaming in takpi via `python-tak-cot-streaming` (takstream), not pytak**, bridged to the central `EventBus` so main takpi logic can react to CoTs (→ LEDs) and hardware can trigger CoTs (button → CotSend).

---

## 1. Why takstream, Not pytak?

| Aspect | pytak | takstream (chosen) |
|--------|-------|---------------------|
| Focus | asyncio transport layer, still raw `xml.etree.ElementTree` | Typed `CotEvent` objects, never XML, streaming-friendly |
| API | Manual XML building, detail blocks as dicts | `CotEvent.marker`/`sa_report`/`chat_message`/`emergency_alert` factories + `set_contact`/`set_group` etc. |
| Connection | User manages `pytak.CotEvent`, `Event` | `CotStream.connect(host,port,cert,key,callsign,team,role,position)` handles identity SA, keepalive pings, graceful `t-x-d-d` delete |
| Keepalive | User implements | Built-in: ping after 15 s idle, `t-x-c-t` with `uid-ping`, dead after 25 s |
| Detail fidelity | User must preserve unknowns | Raw `detail` preserved verbatim, unknown fields survive round-trip |
| Python | 3.8+ | 3.11+ (Poetry, mypy strict) |

takpi's requirement: *"we're **not** using pytak, we're using `../python-tak-cot-streaming` as streaming library, ideally included through a git submodule."* – Implemented as git submodule at `takpi/python-tak-cot-streaming` (relative `../python-tak-cot-streaming`) and path dependency `python-tak-cot-streaming = {path = "../python-tak-cot-streaming", develop = true}` in `common/pyproject.toml:42`.

---

## 2. Submodule Setup

### 2.1 What Was Done

```bash
cd takpi
git init -b main
git -c protocol.file.allow=always submodule add ../python-tak-cot-streaming python-tak-cot-streaming
# .gitmodules:
# [submodule "python-tak-cot-streaming"]
#   path = python-tak-cot-streaming
#   url = ../python-tak-cot-streaming
cat .gitmodules
ls python-tak-cot-streaming/src/takstream/  # cot.py, stream.py, __init__.py
```

Sibling `../python-tak-cot-streaming` (original clone at `~/Dev/TAK/python-tak-cot-streaming`) is the submodule remote (file protocol allowed). Cloning takpi with `--recurse-submodules` will fetch takstream.

### 2.2 Alternative (HTTPS)

If sibling not available (CI), use GitHub URL:

```bash
git submodule add https://github.com/sgofferj/python-tak-cot-streaming.git python-tak-cot-streaming
```

Both give same import `takstream`.

### 2.3 Install

Via `common` venv (per `AGENTS.md:48` – no `--break-system-packages`):

```bash
poetry install --directory common  # installs takstream via path develop
poetry run --directory common python -c "import takstream; print(takstream.__file__)"
# → /home/.../takpi/python-tak-cot-streaming/src/takstream/__init__.py
```

Or at takpi root:

```bash
git clone --recurse-submodules <takpi>
cd takpi/common && poetry install
```

---

## 3. Takstream API Summary (per `python-tak-cot-streaming/README.md:1` and `src/takstream/`)

### 3.1 `CotEvent` (`src/takstream/cot.py:1`)

Typed CoT object: envelope `uid`, `cot_type`, `how`, `time`/`start`/`stale`, `lat`/`lon`/`hae`/`ce`/`le`, `extra_attrs`, `detail` (raw `Element`), plus convenience properties.

| Factory | Example |
|---------|---------|
| `CotEvent.marker(lat,lon,callsign="PIT",remarks="landmark")` | `b-m-p-s-m` marker |
| `CotEvent.sa_report(lat,lon,callsign="MYCALL",team="Cyan",role="Team Member")` | `a-f-G-U-C` SA |
| `CotEvent.chat_message("hi",sender_callsign="ALPHA",sender_uid="ANDROID-123")` | `b-t-f` GeoChat |
| `CotEvent.emergency_alert(lat,lon,callsign="MYCALL",emergency_type="911")` | `b-a-o-tif` |
| `CotEvent.emergency_cancel(uid=...,callsign=...)` | `b-a-o-can` |
| `CotEvent.identity_sa(callsign,team,role,uid,...)` | Initial SA |
| `CotEvent.self_delete(target_uid)` | `t-x-d-d` graceful disconnect |

Detail setters (fluent, upsert, preserve unknown siblings):

```python
event.set_contact("MYCALL")
event.set_group("Cyan", "Team Member")
event.set_track(speed=5.0, course=90.0)
event.set_remarks("some text")
```

Predicates: `is_sa()`, `is_marker()`, `is_chat()`, `is_emergency()`, `is_stale()`, `age()`.

Serialization: `event.to_xml()` / `CotEvent.from_xml(xml)` – round-trip fidelity, `fill_defaults` adds `time`/`start`/`stale`.

### 3.2 `CotStream` (`src/takstream/stream.py:1`)

Async TCP/TLS streaming with background tasks (pump inbound, heartbeat).

| Method | Description |
|--------|-------------|
| `await CotStream.connect(host,port,cert=,key=,ca=,callsign=,team=,role=,position=)` | Open TCP, build `SSLContext` if cert/key given (mTLS), else cleartext; `start_background_tasks()` + `announce_identity()` automatically |
| `async for event in stream:` | Iterate incoming `CotEvent` (via `EventFramer` handling concatenated `<event>...</event>`) |
| `await stream.receive() -> CotEvent | None` | Pull one event or `None` on EOF |
| `await stream.send(event)` | Serialize (`fill_defaults=True` adds stale etc.), auto parent-link for non-self SA/markers |
| `await stream.set_position(lat,lon)` | Update `position` + immediate SA |
| `stream.set_cot_type(type)` | Change own type + re-announce |
| `await stream.close()` | Send `t-x-d-d` delete for self, cancel tasks, close writer |
| `stream.stats` | `{"received","sent","pings_sent"}` |
| `stream.connected` | `not _eof` |

Keepalive: heartbeat tick 0.5 s, ping `t-x-c-t` after 15 s silence, dead after 25 s, SA re-announce every 30 s (`stream.py:28`).

TLS: `cert`+`key` → `SSLContext(PROTOCOL_TLS_CLIENT)`, `load_cert_chain`, optional `ca` verification; no certs → cleartext.

---

## 4. CotBus – Bridge to EventBus (`cot_bus.py:1`)

`CotBus` is the glue so **main process controls LEDs based on CoTs** and **buttons trigger CoTs**, both via `EventBus`.

```
CotStream (takstream, TCP/TLS) ◄──send── CotBus ──subscribe CotSend ◄── EventBus ◄── main/button handlers
      │
      └──receive──► CotBus ──publish CotReceived──► EventBus ──► main handlers ──publish LedCommand──► HardwareManager
```

### 4.1 Events

| Type | Direction | Fields |
|------|-----------|--------|
| `CotReceived` | `CotBus` → bus → main | `cot: CotEvent` (takstream), `raw_xml?` |
| `CotSend` | main → bus → `CotBus` → `CotStream` | `cot: CotEvent` |

Both `@dataclass(frozen=True)`.

### 4.2 CotBus API

| Method | Description |
|--------|-------------|
| `__init__(bus, stream)` | `bus: EventBus`, `stream: CotStream` (or fake with `send`/`__aiter__`) |
| `await start()` | Subscribe to `CotSend` on bus, start `_recv_loop` task |
| `await stop()` | Unsubscribe, cancel recv task |
| `async with CotBus(bus,stream)` | Context manager |
| `await send(cot)` | Helper `bus.publish(CotSend(cot=cot))` |

`_recv_loop` prefers `async for cot in stream` else `while: cot = await stream.receive()`; each `cot` is published as `CotReceived(cot=cot)` on bus.

`_handle_send` calls `await stream.send(cot)` – errors logged, not bubbled.

### 4.3 Example – Bidirectional in `app.py:1`

```python
# In TakPiApp.start():
cot_bus = CotBus(bus, stream)
await cot_bus.start()
bus.subscribe(CotReceived, _on_cot)  # CoT → LED
bus.subscribe(ButtonEvent, _on_button)  # Button → CoT

async def _on_button(evt: ButtonEvent):
    if evt.id == "btn_emergency" and evt.pressed:
        cot = CotEvent.emergency_alert(lat=60, lon=24, callsign="PI-PANEL")
        await bus.publish(CotSend(cot=cot))
        await bus.publish(LedCommand(..., state=True))

async def _on_cot(evt: CotReceived):
    cot = evt.cot
    if cot.is_emergency():
        await bus.publish(LedCommand(id="led_alert", ..., state=True, blink_ms=500))
```

See `app.py:143` for full example handlers (emergency→blink, chat→led, team Cyan→led).

---

## 5. Configuration (CotConfig, `app.py:29`)

| Field | Type | Notes |
|-------|------|-------|
| `host` | `str` | `tak.example.com` placeholder; real from `.env` gitignored |
| `port` | `int` | 8089 TLS or 8087 plaintext |
| `cert` / `key` | `str|None` | Paths to PEM client cert/key for mTLS; both needed together |
| `ca` | `str|None` | CA to verify server (optional; TAK self-signed → often None + `CERT_NONE`) |
| `callsign`/`team`/`role` | `str|None` | All three together → initial SA; else anonymous (no identity, GeoChat not routed) |
| `cot_type` | `str` | Default `a-f-G-U-C` |
| `uid` | `str|None` | Auto UUID4 if None |

Secrets never committed (`.gitignore` + pre-commit `detect-secrets`).

**`.env` example (gitignored):**

```
TAK_HOST=tak.example.com
TAK_PORT=8089
TAK_CERT=certs/client.pem
TAK_KEY=certs/client.key
TAK_CALLSIGN=PI-PANEL
TAK_TEAM=Cyan
TAK_ROLE=Team Member
```

Load via `takpi_common.config.load_env()` + `get_str("TAK_HOST")` etc.

---

## 6. Usage Examples

### 6.1 Minimal – Connect and Print CoTs (takstream only)

```python
import asyncio
from takstream import CotStream

async def main():
    stream = await CotStream.connect(
        "tak.example.com", 8089,
        cert="certs/client.pem", key="certs/client.key",
        callsign="PI-PANEL", team="Cyan", role="Team Member",
    )
    async for event in stream:
        print(event.uid, event.cot_type, event.point.lat, event.point.lon)

asyncio.run(main())
```

### 6.2 Via Bus – Button Press Sends Emergency, Emergency CoT Blinks LED

```python
import asyncio
from takpi_common.bus import EventBus
from takpi_common.mcp23017.driver import FakeSMBus
from takpi_common.mcp23017.manager import HardwareConfig
from takpi_common.mcp23017.io import ButtonConfig, LedConfig
from takpi_common.app import TakPiApp, CotConfig

async def main():
    bus = EventBus()
    hw_cfg = HardwareConfig(
        devices=[0x20],
        buttons=[ButtonConfig(id="btn_emergency", device_addr=0x20, pin=0)],
        leds=[LedConfig(id="led_alert", device_addr=0x20, pin=8)],
        smbus=FakeSMBus(),
    )
    cot_cfg = CotConfig(host="tak.example.com", port=8089, callsign="PI-PANEL", team="Cyan", role="Team Member")

    async with TakPiApp(hardware_cfg=hw_cfg, cot_cfg=cot_cfg, bus=bus):
        # Simulate button press → should send CotSend and light LED
        # (FakeSMBus drives pin low)
        await asyncio.sleep(10)

asyncio.run(main())
```

### 6.3 CoT Factory → Bus

```python
from takstream import CotEvent
from takpi_common.cot_bus import CotSend

cot = CotEvent.marker(lat=48.208, lon=16.373, callsign="PIT", remarks="test")
await bus.publish(CotSend(cot=cot))  # CotBus will send via CotStream
```

---

## 7. Testing (No TAK Server, No Hardware)

| Test | Coverage |
|------|----------|
| `tests/test_cot_bus.py` | Fake stream (AsyncMock) → CotReceived publish, CotSend → stream.send, start/stop lifecycle |
| `tests/test_app.py` | TakPiApp with FakeSMBus + fake stream: button → CotSend, CoT → LedCommand |

Run:

```bash
poetry install --directory common  # installs takstream via path develop
poetry run --directory common pytest tests/test_cot_bus.py tests/test_app.py -v
# All with FakeSMBus/AsyncMock, no I2C, no TAK server, no certs
```

Live integration (real server) is separate: create `.env` with `TAK_LIVE_*` and run `poetry run pytest live_tests -m live` in `python-tak-cot-streaming` (not in takpi).

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `takstream not available` warning in `TakPiApp` | Submodule not cloned or not installed | `git submodule update --init --recursive`; `poetry install --directory common` (path dep develop) |
| `ModuleNotFoundError: takstream` | `common` not installed as package | `poetry install --directory common` or `export PYTHONPATH=common/src:python-tak-cot-streaming/src` |
| `CotStream.connect` raises `FileNotFoundError: cert` | Bad `TAK_CERT` path | Check `.env` `TAK_CERT` absolute or relative to CWD; certs gitignored |
| No `CotReceived` events | No identity SA → server not routing | Provide `callsign`+`team`+`role` together in `CotConfig`; check `stream.stats` |
| `CotSend` not sent | `CotBus` not started or handler exception | Ensure `await cot_bus.start()` before publish; check logs for `CotBus send failed` |
| CoT→LED not blinking | Wrong `LedCommand.id` or no `HardwareManager` subscriber | Verify `bus.subscribed(LedCommand)` >0; `HardwareManager.start()` subscribes; `LedConfig.id` must match command `id` |

---

## 9. References

- Submodule: `takpi/python-tak-cot-streaming` (git submodule `../python-tak-cot-streaming`, `https://github.com/sgofferj/python-tak-cot-streaming.git`)
- Library docs: `python-tak-cot-streaming/README.md:1`, `python-tak-cot-streaming/src/takstream/cot.py:1` (`CotEvent`), `python-tak-cot-streaming/src/takstream/stream.py:1` (`CotStream`, `RX_STALE_SECONDS` etc.)
- Bridge: `takpi_common/cot_bus.py:1` (`CotBus`, `CotReceived`, `CotSend`)
- Bus: `takpi_common/bus.py:1` (`EventBus`)
- App wiring: `takpi_common/app.py:1` (`TakPiApp`, handlers `_on_button`/`_on_cot`)
- Hardware: `README_mcp23017.md:1` (MCP23017, `LedCommand`), `README_bus.md:1` (bidirectional flow)
- Chronometer contrast (GPIO UART, not takstream): `tak-bridge-chronometer/README_chronometer.md:1`
- Tests: `common/tests/test_cot_bus.py`, `common/tests/test_app.py`
- AGENTS.md: TakStream usage note (replaces pytak), Pi I2C vs GPIO UART distinction

---

## 10. Changelog

| Version | Date | Notes |
|---------|------|-------|
| 0.1.0 | 2026-09-07 | Initial CotBus + TakPiApp integration – git submodule `python-tak-cot-streaming` (takstream, not pytak), `CotReceived`/`CotSend` on `EventBus`, CoT→LED and button→CoT examples, `mypy --strict` clean |

