# README_mcp23017 – MCP23017 Daisy-Chain Framework

> **Path:** `takpi/common/src/takpi_common/mcp23017/`  
> **Driver:** `driver.py:1` (`MCP23017`, `FakeSMBus`) – register-level I2C at 0x20-0x27  
> **IO:** `io.py:1` (`ButtonConfig`/`EncoderConfig`/`LedConfig`, `ButtonEvent`/`EncoderEvent`/`LedCommand`)  
> **Manager:** `manager.py:1` (`HardwareManager`, `HardwareConfig`) – polling, debounce, quadrature, blink  
> **Bus:** `../bus.py:1` (`EventBus`) – hardware ↔ main bidirectional  
> **Tests:** `tests/test_driver.py`, `tests/test_manager.py` (FakeSMBus, no hardware)

This is the thorough reference for the **MCP23017 daisy-chain framework** that lets takpi read buttons/encoders and drive LEDs via multiple MCP23017 I/O expanders on one Pi I2C bus, with events flowing through the central `EventBus`.

---

## 1. Purpose & Scope

**Goal:** Provide a framework to read **buttons**, **rotary encoders** (quadrature + push) and control **LEDs** via **daisy-chained MCP23017** modules on the Pi's I2C bus (`/dev/i2c-1`, `SCL` GPIO3 pin 5, `SDA` GPIO2 pin 3), with:

- Buttons/encoders → publish typed events on `EventBus` → main takpi process can subscribe and trigger actions (send CoT via `CotBus`, toggle other LEDs, etc.)
- Main process → publish `LedCommand` on `EventBus` → `HardwareManager` drives MCP23017 outputs → LEDs reflect application state (e.g., `CotReceived` with emergency `b-a-o-tif` → blink alert LED)

**Why MCP23017?**
- 16 GPIOs per chip (GPA0-7, GPB0-7), 8 addresses via A0-A2 → 128 GPIOs max on one bus (8 × 16)
- 3.3V/5V tolerant, 100 kΩ pull-ups built-in, interrupt-on-change with INTA/INTB (optional Pi GPIO interrupt)
- Cheap, widely available, daisy-chain by sharing SDA/SCL + distinct address straps

**Scope:** Pure I2C driver + high-level Button/Encoder/LED abstractions + `HardwareManager` orchestration. No CoT logic here (see `README_cot.md:1` and `README_bus.md:1`); CoT→LED and Button→CoT wiring is done in `app.py:1`.

**Not pytak:** Uses `../python-tak-cot-streaming` (takstream) via git submodule `takpi/python-tak-cot-streaming` (url `../python-tak-cot-streaming`), not `pytak`.

---

## 2. Hardware Reference

### 2.1 MCP23017 Basics

| Item | Value |
|------|-------|
| Part | Microchip MCP23017-E/SP (DIP) or -SO (SOIC) |
| I2C bus | Pi `/dev/i2c-1` (BCM2 SDA pin 3, BCM3 SCL pin 5, plus GND pin 6) |
| Addresses | `0x20`–`0x27` via `A0`/`A1`/`A2` tied to GND/VCC (`A0=LSB`). Example: all GND → `0x20`, A0=VCC → `0x21` |
| GPIOs | 16 per chip: GPA0-7 (pins 21-28), GPB0-7 (pins 1-8) → logical pins `0-15` (`0-7` GPA, `8-15` GPB) |
| Registers BANK=0 | `IODIRA/B` 0x00/01 (direction), `GPPUA/B` 0x0C/0D (pull-up), `GPIOA/B` 0x12/13, `OLATA/B` 0x14/15, `GPINTENA/B`, `INTFA/B`, `INTCAPA/B`, `IOCON` 0x0A |
| Interrupts | `INTA` (pin 20) for GPA, `INTB` (pin 19) for GPB; can mirror via `IOCON.MIRROR` and wire to Pi BCM17 (config `interrupt_gpio`) |
| Power | 3.3V from Pi (pin 1 or 17) or 5V (pin 2) – MCP is 3.3/5V tolerant; decouple 100 nF + 10 µF near VDD |

### 2.2 Daisy-Chain Wiring (Shared Bus)

```
Pi 40-pin Header              MCP23017 #0 (0x20) A0=A1=A2=GND    MCP23017 #1 (0x21) A0=VCC
----------------            ------------------------------      ------------------------------
Pin 1  3.3V ─────────────── VDD (pin 9)                       VDD
Pin 3  GPIO2 SDA ─────────── SDA (pin 13) ──────────────────── SDA (shared, 4.7k pull-up to 3.3V if not on board)
Pin 5  GPIO3 SCL ─────────── SCL (pin 12) ──────────────────── SCL (shared, 4.7k pull-up)
Pin 6  GND ───────────────── VSS (pin 10) + A0/A1/A2 + GND ──── VSS + A1/A2 GND, A0 VCC
                            GPA0 (21) ─ Button1 → GND         GPA0 ─ LED1 → resistor → GND
                            GPB0 (1)  ─ Encoder A ─┐          GPB0 ─ LED2
                            GPB1 (2)  ─ Encoder B ─┤ Quadrature
                            GPB2 (3)  ─ Encoder Btn┘ → GND
                            INTA (20) ─ (optional) Pi BCM17 pin 11
```

- **Shared:** SDA, SCL, GND, VDD across all chips.
- **Distinct:** A0-A2 strapping per board → address. Use 3-pin header with jumpers.
- **Pull-ups:** Enable internal 100 kΩ via `GPPU` (no external resistor for buttons/encoders). For LEDs, drive output low/high with series resistor (220 Ω–1 kΩ).
- **Decoupling:** 100 nF ceramic at each MCP VDD.
- **Bus length:** I2C <1 m total; for longer, use lower pull-up (2.2 k) or I2C extender.

**Enable I2C on Pi:**

```bash
sudo raspi-config → Interface Options → I2C → Yes
# or: echo "dtparam=i2c_arm=on" | sudo tee -a /boot/firmware/config.txt
sudo reboot
ls -l /dev/i2c-1  # crw-rw---- root i2c
groups  # must include i2c
sudo usermod -a -G i2c $USER; newgrp i2c
i2cdetect -y 1  # should show 20,21,... for each board
#  0 1 2 3 4 5 6 7 8 9 a b c d e f
# 00: -- -- -- -- -- -- -- -- -- -- -- -- --
# 10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
# 20: 20 21 -- -- -- -- -- -- -- -- -- -- -- -- -- --
```

### 2.3 Button / Encoder / LED Wiring Details

**Button (active-low, pull-up):**
```
MCP GPA0 ──┬── Button ── GND
           └── (internal 100k to 3.3V via GPPUA)
```
- Config: `ButtonConfig(id="btn1", device_addr=0x20, pin=0, pullup=True, debounce_ms=50)`
- Raw high = released (pull-up), low = pressed (GND). Debounce 50 ms default.

**Encoder (quadrature + push):**
```
MCP GPB0 ── Encoder A ── GND (with pullup)
MCP GPB1 ── Encoder B ── GND
MCP GPB2 ── Encoder Button → GND
```
- Two pins A/B generate Gray code `00→01→11→10→00` per detent. Manager decodes via table `ENCODER_TABLE` (`io.py:73`). Emits `EncoderEvent(delta=+1/-1)` per edge (detent = 4 edges) or per detent depending on `accum` config.
- Optional push-button debounced like normal button → `EncoderButtonEvent`.

**LED (active-high, to GND):**
```
MCP GPA7 ─── 470Ω ── LED anode → LED cathode → GND
```
- Config: `LedConfig(id="led1", device_addr=0x20, pin=7, initial=False)`. `invert=True` for active-low (to VCC) or transistor driver.

---

## 3. Daisy-Chain Concept

"Daisy-chained" here means **multiple MCP23017 share SDA/SCL** with distinct addresses; not SPI daisy-chain with shift register. Each board is independently addressable:

- **Up to 8** on one Pi I2C bus (A0-A2). For more, use second bus (`/dev/i2c-0` on Pi 4 `dtoverlay=i2c0`) or I2C mux (TCA9548A).
- **Manager owns one `smbus` object** shared across `MCP23017` instances (see `manager.py:103` – `smbus_obj` shared, locked via `asyncio.Lock` for thread-safety).
- Polling reads all devices' 16-bit `GPIO` in parallel (per-device `read_gpio()` via `read_word_data`), then decodes per-pin.

**Example chain of 3:**

```python
cfg = HardwareConfig(
    i2c_bus=1,
    devices=[0x20, 0x21, 0x27],  # A0/A1/A2 = 000,001,111
    buttons=[
        ButtonConfig(id="btn_arm", device_addr=0x20, pin=0),
        ButtonConfig(id="btn_panic", device_addr=0x21, pin=3),
    ],
    encoders=[
        EncoderConfig(id="enc_freq", device_addr=0x20, pin_a=1, pin_b=2, pin_button=3),
    ],
    leds=[
        LedConfig(id="led_armed", device_addr=0x27, pin=0),
        LedConfig(id="led_alert", device_addr=0x27, pin=1),
    ],
    poll_interval_ms=5,
)
```

---

## 4. Configuration (HardwareConfig)

`manager.py:48` `HardwareConfig` is the single source of truth, declarative and serializable (e.g., from YAML).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `i2c_bus` | `int` | `1` | Pi I2C bus number (`/dev/i2c-1` → `1`) |
| `devices` | `list[int]` | `[0x20]` | Addresses 0x20-0x27, must match A0-A2 straps. Manager creates one `MCP23017` per address |
| `buttons` | `list[ButtonConfig]` | `[]` | Each `id` must be unique (used for `ButtonEvent.id` + LED mapping) |
| `encoders` | `list[EncoderConfig]` | `[]` | `pin_a`, `pin_b` on same device (recommended for efficient word read) |
| `leds` | `list[LedConfig]` | `[]` | Output pins; `initial` sets startup state |
| `poll_interval_ms` | `int` | `5` | Poll period (5 ms → 200 Hz, enough for encoders; buttons debounce 50 ms) |
| `debounce_ms` | `int` | `50` | Default debounce for buttons (overridden per `ButtonConfig.debounce_ms`) |
| `interrupt_gpio` | `int | None` | BCM pin wired to MCP INT (future: interrupt-driven instead of pure polling) |
| `smbus` | `object | None` | Injection for tests – `FakeSMBus()` or `smbus2.SMBus(1)` |

**Validation:** `MCP23017.__init__` checks `0x20 <= addr <= 0x27`; `Button`/`Encoder`/`Led` check `0 <= pin <= 15`.

**Example YAML (for `common/examples/hardware.yaml`):**

```yaml
i2c_bus: 1
devices: [0x20, 0x21]
poll_interval_ms: 5
buttons:
  - id: btn1
    device_addr: 0x20
    pin: 0
    debounce_ms: 50
encoders:
  - id: enc1
    device_addr: 0x20
    pin_a: 1
    pin_b: 2
    pin_button: 3
leds:
  - id: led_alert
    device_addr: 0x21
    pin: 0
  - id: led_chat
    device_addr: 0x21
    pin: 1
```

Load via `HardwareConfig(i2c_bus=..., devices=[...], buttons=[ButtonConfig(**b) for b in yaml["buttons"]])`.

---

## 5. Public API Reference

### 5.1 `MCP23017` (`driver.py:134`)

Low-level, synchronous I2C driver; manager wraps in `run_in_executor` for asyncio.

| Method | Signature | Description |
|--------|-----------|-------------|
| `__init__` | `(address=0x20, bus=1, smbus=None)` | Bus number + address 0x20-0x27, inject `FakeSMBus` for tests |
| `setup` | `(iodir_a=0xFF, iodir_b=0x00, gppu_a=0x00, ...)` | Init IOCON (SEQOP/BANK/MIRROR 0), IODIR, GPPU, clear GPINTEN |
| `set_direction` | `(pin, is_input)` | `IODIRA/B` – True=input, False=output |
| `set_pullup` | `(pin, enable)` | `GPPUA/B` 100 k |
| `set_polarity` | `(pin, inverted)` | `IPOLA/B` |
| `read_gpio` | `() -> int` | 16-bit `GPIOB<<8|GPIOA` (via `read_word_data`) |
| `write_gpio` | `(value)` | 16-bit `OLATB<<8|OLATA` |
| `read_pin` / `write_pin` | `(pin, value)` | Single pin RMW on OLAT/GPIO |
| `enable_interrupt` etc. | `(pin, compare_to_defval, defval)` | `GPINTEN`/`INTCON`/`DEFVAL` + `IOCON` mirror |
| `read_int_flags` / `read_int_captured` | `() -> int` | `INTF`/`INTCAP` |
| `FakeSMBus` | `(bus_id=1)` | In-memory fake: `write_byte_data`, `read_byte_data`, `read_word_data`, `set_input(addr,pin,bool)`, `get_olat(addr,pin)` |

All operations are **blocking** (smbus2). `HardwareManager` runs them in executor under `asyncio.Lock` to serialize bus access.

### 5.2 `Button` / `Encoder` / `LED` (`io.py:1`)

Pure data + helpers, no I2C.

| Class/Config | Fields | Event |
|--------------|--------|-------|
| `ButtonConfig` | `id, device_addr, pin, pullup=True, invert=False, debounce_ms=50` | – |
| `ButtonEvent` | `id, device_addr, pin, pressed, timestamp` | `released` property |
| `ButtonState` | `cfg, last_raw, last_stable, pressed` | `update(raw_high, now) -> ButtonEvent | None` (debounce) |
| `EncoderConfig` | `id, device_addr, pin_a, pin_b, pin_button?, pullup=True` | – |
| `EncoderEvent` | `id, device_addr, delta (+1/-1), position, timestamp` | – |
| `EncoderState` | `cfg, position, _prev_state, _accum` | `update(a_high,b_high,now) -> EncoderEvent | None` (Gray table `ENCODER_TABLE`) |
| `LedConfig` | `id, device_addr, pin, initial=False, invert=False` | – |
| `LedCommand` | `id, device_addr, pin, state, blink_ms?` | Main → hardware (via bus) |
| `LedState` | `id, device_addr, pin, state, timestamp` | Hardware → bus ack after write |

**Debounce:** `ButtonState` tracks `last_raw`, `last_change_ts`, `last_stable`. Edge emitted only after `now - last_change_ts >= debounce_ms` and `active != last_stable`. Initial release on boot is **not** emitted (avoids spam).

**Quadrature:** `EncoderState._ENCODER_TABLE` maps `prev<<2 | curr` (4-bit) to delta; `position += delta`. Emits per edge (detent = 4 edges); accumulate if per-detent needed.

### 5.3 `HardwareManager` (`manager.py:64`)

| Method | Description |
|--------|-------------|
| `__init__(config, bus)` | Builds `MCP23017` per `config.devices` (shared smbus), `ButtonState`/`EncoderState`/`LedRuntime`, no I/O yet |
| `start()` | `setup()` each MCP (IODIR/GPPU), configure buttons/encoders as inputs+pullup, LEDs as outputs+initial, subscribe to `LedCommand` on bus, start `_poll_loop` task |
| `stop()` | Cancel poll + blink tasks, unsubscribe, close devices |
| `async with HardwareManager(cfg,bus)` | Context manager |
| `_poll_loop()` | Every `poll_interval_ms` (5 ms) → `_poll_once(now)` |
| `_poll_once(now)` | Snapshot `read_gpio()` per device (word read), update `ButtonState`→publish `ButtonEvent`, `EncoderState`→`EncoderEvent`, `EncoderButtonEvent` |
| `_handle_led_command(cmd)` | Lookup `LedRuntime` by `id` or `(addr,pin)`, cancel previous blink, if `blink_ms` start `_blink_led` task else `write_pin` + publish `LedState` |
| `_blink_led(rt,cmd)` | Toggles pin every `blink_ms/2`, publishes `LedState` each half, restores `cmd.state` on cancel |
| `set_led(led_id, state)` | Helper `bus.publish(LedCommand(...))` |

**Daisy-chain specifics:** `poll_interval_ms` 5 ms → 200 Hz poll across all devices (e.g., 3 devices → 3 word reads per 5 ms → 600 I2C transactions/s, trivial at 100 kHz/400 kHz bus). `asyncio.Lock` serializes I2C; `run_in_executor` avoids blocking event loop.

**Example wiring check after `start()`:**

```bash
# Polling logs
INFO HardwareManager started on I2C bus 1 with 2 devices, 2 buttons, 1 encoders, 2 leds (poll 5 ms)
# Button press → log via bus subscriber
INFO Button btn1 pressed
INFO Encoder enc1 delta 1 pos 42
```

---

## 6. Usage Examples

### 6.1 Minimal – One Button + One LED on Single MCP (0x20)

```python
import asyncio
from takpi_common.bus import EventBus
from takpi_common.mcp23017.driver import FakeSMBus
from takpi_common.mcp23017.io import ButtonConfig, LedConfig
from takpi_common.mcp23017.manager import HardwareConfig, HardwareManager

async def main():
    bus = EventBus()
    fake = FakeSMBus()
    cfg = HardwareConfig(
        devices=[0x20],
        buttons=[ButtonConfig(id="btn1", device_addr=0x20, pin=0)],
        leds=[LedConfig(id="led1", device_addr=0x20, pin=8)],
        smbus=fake,
    )
    async with HardwareManager(cfg, bus):
        # hardware → main
        bus.subscribe(ButtonEvent, lambda e: print(f"button {e.id} {e.pressed}"))
        # main → hardware: turn LED on when button pressed
        async def on_btn(e):
            if e.id == "btn1":
                await bus.publish(LedCommand(id="led1", device_addr=0x20, pin=8, state=e.pressed))
        bus.subscribe(ButtonEvent, on_btn)

        # Simulate press: drive GPA0 low (active-low)
        fake.set_input(0x20, 0, False)
        await asyncio.sleep(0.1)  # poll + debounce
        print("OLAT led state:", fake.get_olat(0x20, 8))

asyncio.run(main())
```

### 6.2 Daisy-Chained – Two Expanders + Encoder

```python
from takpi_common.mcp23017.io import EncoderConfig

cfg = HardwareConfig(
    i2c_bus=1,
    devices=[0x20, 0x21],  # 0x20: inputs, 0x21: outputs
    buttons=[ButtonConfig(id="btn_arm", device_addr=0x20, pin=0)],
    encoders=[EncoderConfig(id="enc_freq", device_addr=0x20, pin_a=1, pin_b=2, pin_button=3)],
    leds=[
        LedConfig(id="led_armed", device_addr=0x21, pin=0),
        LedConfig(id="led_alert", device_addr=0x21, pin=1, initial=False),
    ],
    poll_interval_ms=5,
    smbus=FakeSMBus(),  # replace with None for real hardware
)

# Bidirectional via bus: button → CoT, CoT → LED (see README_cot.md)
# Encoder → LED example
from takpi_common.mcp23017.io import EncoderEvent
bus.subscribe(EncoderEvent, lambda e: bus.publish(
    LedCommand(id="led_alert", device_addr=0x21, pin=1, state=e.delta>0)
))
```

### 6.3 Direct Packet Inspection (No Hardware)

```python
from takpi_common.mcp23017.driver import MCP23017, FakeSMBus

fake = FakeSMBus()
dev = MCP23017(address=0x20, smbus=fake)
dev.setup(iodir_a=0xFF, iodir_b=0x00)  # A inputs, B outputs
dev.set_pullup(0, True)
fake.set_input(0x20, 0, False)  # button press
assert dev.read_pin(0) == False
dev.write_pin(8, True)  # LED on
assert fake.get_olat(0x20, 8) is True
```

---

## 7. Testing (No Hardware)

| Test File | Coverage |
|-----------|----------|
| `tests/test_driver.py` | FakeSMBus R/W, IODIR/GPPU, GPIO/OLAT, word read, pin RMW, interrupts |
| `tests/test_manager.py` | HardwareManager start/stop, button debounce → ButtonEvent, encoder quadrature → EncoderEvent, LedCommand → OLAT, blink task, daisy-chain multi-device poll |

Run via venv (per `AGENTS.md:48` – no `--break-system-packages`):

```bash
poetry install --directory common
poetry run --directory common pytest -v  # with FakeSMBus, no I2C
poetry run --directory common black --check src tests
poetry run --directory common mypy src  # strict
poetry run --directory common pylint src  # 10.00
```

CI uses `FakeSMBus`; no Pi, no I2C, no `smbus2` hardware required.

### 7.1 Mock Pattern (tests)

```python
fake = FakeSMBus()
cfg = HardwareConfig(devices=[0x20,0x21], buttons=[...], smbus=fake)
mgr = HardwareManager(cfg, bus)
await mgr.start()
fake.set_input(0x20, 0, False)  # press
await asyncio.sleep(0.06)  # debounce + poll
assert fake.get_olat(0x21, 0) is True
```

---

## 8. Troubleshooting (MCP Chain)

| Symptom | Cause | Fix |
|---------|-------|-----|
| `i2cdetect -y 1` shows no `20/21` | Wrong A0-A2 strap or power | Check VSS/GND, A0-A2 to GND/VCC, VDD 3.3V, 100 nF decoupling, SDA/SCL continuity |
| `Permission denied /dev/i2c-1` | Not in `i2c` group | `sudo usermod -a -G i2c pi; newgrp i2c; ls -l /dev/i2c-1` |
| `OSError: [Errno 121] Remote I/O error` | No ack at address (wrong addr or no pull-ups) | Verify `i2cdetect`, check 4.7k pull-ups on SDA/SCL (one pair per bus, often on Pi) |
| Buttons always high | Pull-up not enabled or button not to GND | Check `set_pullup`, wiring: MCP pin → button → GND, not VCC |
| Encoder missed steps | Poll interval too slow or bus contended | Reduce `poll_interval_ms` to 2-5 ms, check other I2C traffic; use `read_word_data` (manager does) |
| LED not lighting | Pin still input or wrong polarity | Check `set_direction(pin,False)`, `LedConfig.invert`, series resistor, LED orientation |
| Blink task not stopping | Missing `await stop()` | Ensure `HardwareManager` `async with` or `await mgr.stop()` cancels blink tasks |

---

## 9. Interrupt Mode (Future)

`HardwareConfig.interrupt_gpio` (BCM pin, e.g., 17) wired to MCP `INTA`/`INTB` (with `IOCON.MIRROR=1` to combine). Manager could `await GPIO.wait_for_edge` instead of pure polling, still polls after interrupt to debounce. Currently polling-only; interrupt is optional optimization for low-CPU.

---

## 10. References

- Datasheet: Microchip MCP23017 (16-bit I/O, I2C, 100 kHz/400 kHz/1.7 MHz)
- Pi I2C: `raspi-config` → I2C, `/dev/i2c-1` (GPIO2/3), `dtparam=i2c_arm=on`
- Bus: `takpi_common/bus.py:1` (`EventBus`) – hardware ↔ main
- App: `takpi_common/app.py:1` (`TakPiApp`) – wiring to `CotBus`
- Chronometer (GPIO UART contrast): `tak-bridge-chronometer/README_chronometer.md:2.1` – shows GPIO UART vs this I2C bus
- Submodule: `takpi/python-tak-cot-streaming` (git submodule `../python-tak-cot-streaming`) – CoT→LED via `README_cot.md:1`
- Tests: `common/tests/test_driver.py`, `common/tests/test_manager.py`

---

## 11. Changelog

| Version | Date | Notes |
|---------|------|-------|
| 0.1.0 | 2026-09-07 | Initial MCP23017 daisy-chain framework – `FakeSMBus`, `MCP23017` driver, `Button`/`Encoder`/`Led` io, `HardwareManager` with polling + debounce + quadrature + blink, `EventBus` integration, `mypy --strict` clean |

