# pylint: disable=too-many-nested-blocks,too-many-branches
"""Wipe module – secure delete of sensitive data on long button press.

Recommended: put the wipe button directly on a GPIO header pin (e.g. header
18 → BCM24) for reliability – not via MCP23017. The manager handles both,
but header is preferred (no I2C dependency, survives MCP failure).

Registers sensitive paths/config keys via :class:`WipeRegistry`; on 10s
continuous button press (`wipe`/`btn_wipe` on header/MCP) wipes:

- Certificates/keys (`certs/*`, `*.pem`/`*.key`/`*.p12`/`*.pfx` plus any
  registered paths)
- Registered config keys (`tak.host`, `tak.cert`, `location` etc.) from
  ``~/takpi/config.yaml`` (only registered keys, not entire file)
- Registered cache/temp dirs/files (``__pycache__``, ``*.pyc``, ``/tmp/tak-*``,
  module-specific)
- Journal (``journalctl --rotate`` + ``--vacuum-time=1s`` if available, plus
  ``/run/log/journal``/``/var/log/journal``)

Then immediate ``poweroff`` via ``sudo poweroff`` / ``systemctl poweroff``.

Modules should register sensitive data at import::

    from takpi_common.wipe import WipeRegistry

    WipeRegistry.register_path(Path("certs/client.pem"), "TAK cert")
    WipeRegistry.register_config_key("tak.host")
    WipeRegistry.register_config_key("location")

See ``AGENTS.md:68`` and ``README.md:51`` for registration note.

Button handling: ``ButtonEvent(id="wipe"|"btn_wipe", pressed)`` on ``EventBus``.
Press ≥10s → wipe, release <10s → cancel. No confirmation.

Testable with ``dry_run=True`` and ``Fake`` poweroff.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess  # nosec B404
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar

from takpi_common.bus import EventBus
from takpi_common.mcp23017.io import ButtonEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry – modules register sensitive data
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SensitivePath:
    path: Path
    description: str = ""
    is_dir: bool = False


@dataclass
class WipeRegistry:
    """Global registry for sensitive paths and config keys."""

    _paths: list[SensitivePath] = field(default_factory=list)
    _config_keys: list[str] = field(default_factory=list)

    # Class-level singletons (avoid multiple instances)
    _global_paths: ClassVar[list[SensitivePath]] = []  # type: ignore
    _global_config_keys: ClassVar[list[str]] = []  # type: ignore

    @classmethod
    def register_path(
        cls, path: str | Path, description: str = "", is_dir: bool | None = None
    ) -> None:
        """Register a sensitive file/dir to be wiped.

        Args:
            path: file or dir path (may contain globs like ``certs/*`` or ``*.pem`` – expanded at wipe time)
            description: human readable
            is_dir: force dir handling, else auto-detected
        """
        p = Path(path)
        # Auto-detect is_dir from trailing slash or existing dir, but allow override
        if is_dir is None:
            is_dir = p.suffix == "" and not p.name.startswith("*")
        entry = SensitivePath(path=p, description=description, is_dir=is_dir)
        # Avoid duplicates
        if entry not in cls._global_paths:
            cls._global_paths.append(entry)
            logger.debug("WipeRegistry: registered path %s (%s)", p, description)

    @classmethod
    def register_config_key(cls, key: str, description: str = "") -> None:
        """Register a sensitive config key (dot-notation, e.g. ``tak.host``, ``location``)."""
        if key not in cls._global_config_keys:
            cls._global_config_keys.append(key)
            logger.debug(
                "WipeRegistry: registered config key %s (%s)", key, description
            )

    @classmethod
    def get_paths(cls) -> list[SensitivePath]:
        return list(cls._global_paths)

    @classmethod
    def get_config_keys(cls) -> list[str]:
        return list(cls._global_config_keys)

    @classmethod
    def clear(cls) -> None:
        """Clear registry (for tests)."""
        cls._global_paths.clear()
        cls._global_config_keys.clear()


# Pre-register common sensitive defaults (per spec: thorough)
# Certificates/keys – file patterns
WipeRegistry.register_path("certs", "certs directory", is_dir=True)
WipeRegistry.register_path("certs/*", "certs files", is_dir=False)
WipeRegistry.register_path("*.pem", "PEM files")
WipeRegistry.register_path("*.key", "KEY files")
WipeRegistry.register_path("*.p12", "P12 files")
WipeRegistry.register_path("*.pfx", "PFX files")
WipeRegistry.register_path("*.crt", "CRT files")
WipeRegistry.register_path("*.cert", "CERT files")
# Config keys – only registered ones will be wiped from YAML
WipeRegistry.register_config_key("tak.host", "TAK host")
WipeRegistry.register_config_key("tak.port", "TAK port")
WipeRegistry.register_config_key("tak.cert", "TAK cert path")
WipeRegistry.register_config_key("tak.key", "TAK key path")
WipeRegistry.register_config_key("tak.ca", "TAK CA")
WipeRegistry.register_config_key("tak.callsign", "TAK callsign")
WipeRegistry.register_config_key("location", "location (lat/lon)")
WipeRegistry.register_config_key("location.latitude", "location lat")
WipeRegistry.register_config_key("location.longitude", "location lon")
WipeRegistry.register_config_key("location.altitude", "location alt")


# ---------------------------------------------------------------------------
# Secure overwrite helpers
# ---------------------------------------------------------------------------
def _secure_overwrite_file(path: Path, passes: int = 1) -> bool:
    """Overwrite file with zeros (once) then unlink. Return True if wiped."""
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
        # Overwrite
        try:
            with open(path, "r+b") as f:  # pylint: disable=unspecified-encoding
                for _ in range(passes):
                    f.seek(0)
                    # Write zeros
                    chunk = b"\x00" * 4096
                    written = 0
                    while written < size:
                        to_write = min(4096, size - written)
                        f.write(chunk[:to_write])
                        written += to_write
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
        except (OSError, PermissionError) as exc:
            logger.debug("Overwrite failed for %s: %s", path, exc)
        # Unlink
        try:
            path.unlink()
            logger.info("Wiped file %s", path)
            return True
        except FileNotFoundError:
            return False
        except (OSError, PermissionError) as exc:
            logger.warning("Failed to unlink %s: %s", path, exc)
            return False
    except (OSError, PermissionError) as exc:
        logger.warning("Wipe file %s failed: %s", path, exc)
        return False


def _wipe_path(
    pattern: Path, base_dir: Path | None = None
) -> int:  # pylint: disable=too-many-nested-blocks,too-many-branches
    """Wipe files matching pattern (glob) with overwrite. Return count."""
    count = 0
    # Resolve base
    if base_dir is None:
        base_dir = Path.cwd()
    # Handle absolute vs relative, and globs
    pat_str = str(pattern)
    # If pattern is absolute, use as is; else join with base_dir
    if pattern.is_absolute():
        # For absolute, check directly or glob
        if "*" in pat_str or "?" in pat_str:
            for p in Path("/").glob(pat_str.lstrip("/")):
                # Need to handle absolute glob correctly – use Path.glob on root
                # Simpler: use Path.glob on parent
                pass
            # Use pathlib's glob for absolute via Path(pattern).parent.glob
            try:
                for p in Path(pat_str).parent.glob(Path(pat_str).name):
                    if p.is_file():
                        if _secure_overwrite_file(p):
                            count += 1
                    elif p.is_dir():
                        count += _wipe_dir(p)
            except (OSError, PermissionError):
                pass
        else:
            p = Path(pat_str)
            if p.is_file():
                if _secure_overwrite_file(p):
                    count += 1
            elif p.is_dir():
                count += _wipe_dir(p)
    else:
        # Relative – glob from base_dir and also from common locations
        # Try base_dir, then home, then cwd
        search_bases = [base_dir, Path.home(), Path.cwd(), Path.home() / "takpi"]
        seen: set[Path] = set()
        for base in search_bases:
            try:
                for p in base.glob(pat_str):
                    if p.resolve() in seen:
                        continue
                    seen.add(p.resolve())
                    if p.is_file():
                        if _secure_overwrite_file(p):
                            count += 1
                    elif p.is_dir():
                        count += _wipe_dir(p)
            except (OSError, PermissionError):
                continue
        # Also try direct path without glob (e.g., "certs" dir)
        direct = base_dir / pattern
        if direct.exists() and direct.resolve() not in seen:
            if direct.is_file():
                if _secure_overwrite_file(direct):
                    count += 1
            elif direct.is_dir():
                count += _wipe_dir(direct)
    return count


def _wipe_dir(path: Path) -> int:
    """Recursively wipe dir contents with overwrite, then rmdir. Return count."""
    count = 0
    if not path.is_dir():
        return count
    try:
        for child in path.rglob("*"):
            if child.is_file():
                if _secure_overwrite_file(child):
                    count += 1
        # Remove empty dirs
        try:
            shutil.rmtree(path)
            logger.info("Wiped dir %s", path)
            count += 1
        except (OSError, PermissionError) as exc:
            logger.warning("Failed to rmdir %s: %s", path, exc)
    except (OSError, PermissionError) as exc:
        logger.warning("Wipe dir %s failed: %s", path, exc)
    return count


def _wipe_config_keys(config_path: Path, keys: list[str]) -> int:
    """Remove registered keys from YAML config file (overwrite). Return count of removed keys."""
    if not config_path.is_file():
        logger.debug("Config %s not found, skip config wipe", config_path)
        return 0
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.warning("PyYAML not installed, cannot wipe config %s", config_path)
        return 0
    try:
        text = config_path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) if text.strip() else {}
        if not isinstance(data, dict):
            logger.warning("Config %s not a mapping, skip", config_path)
            return 0
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("Failed to load config %s for wipe: %s", config_path, exc)
        return 0

    removed = 0
    # Handle dot-notation keys, e.g., "tak.host" → data["tak"]["host"]
    for key in keys:
        parts = key.split(".")
        cur: Any = data
        found = True
        for part in parts[:-1]:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                found = False
                break
        if not found:
            continue
        last = parts[-1]
        if isinstance(cur, dict) and last in cur:
            del cur[last]
            removed += 1
            logger.info("Wiped config key %r from %s", key, config_path)
            # Clean up empty parent dicts (e.g., if "tak" becomes empty, remove it)
            # Walk back and remove empty dicts
            for i in range(len(parts) - 2, -1, -1):
                parent_parts = parts[: i + 1]
                parent: Any = data
                for p in parent_parts[:-1]:
                    parent = parent.get(p, {})
                key_to_check = parent_parts[-1]
                if (
                    isinstance(parent, dict)
                    and key_to_check in parent
                    and isinstance(parent[key_to_check], dict)
                    and not parent[key_to_check]
                ):
                    del parent[key_to_check]
                    logger.debug(
                        "Removed empty config section %r", ".".join(parent_parts)
                    )

    if removed > 0:
        # Overwrite file then write new content
        try:
            # Secure overwrite old file first
            _secure_overwrite_file(config_path)
            # Write new (may be empty)
            if data:
                new_text = yaml.safe_dump(
                    data, default_flow_style=False, sort_keys=False
                )
                config_path.write_text(new_text, encoding="utf-8")
            else:
                # If all wiped, leave empty or minimal
                config_path.write_text("", encoding="utf-8")
            logger.info("Wiped %d config keys from %s", removed, config_path)
        except (OSError, PermissionError) as exc:
            logger.warning("Failed to write wiped config %s: %s", config_path, exc)
            return 0
    return removed


# ---------------------------------------------------------------------------
# Wipe Manager – button handling and wipe execution
# ---------------------------------------------------------------------------
class WipeManager:
    """Manages wipe on long button press (10s).

    Recommended: put the wipe button on a GPIO header pin (e.g. header 18
    → BCM24) for reliability – survives I2C/MCP failure. MCP fallback
    (`btn_wipe` on `0x20: GPA0`) also works via ``ButtonEvent`` on ``EventBus``.

    Example::

        bus = EventBus()
        wipe = WipeManager(bus, poweroff_func=fake_poweroff)
        await wipe.start()
        # ButtonEvent on bus with id wipe/btn_wipe triggers wipe after 10s hold
        # or direct GPIO header 18 monitoring if configured in ``hardware.gpio``
    """

    WIPE_HOLD_S = 10.0
    BUTTON_IDS = ("wipe", "btn_wipe", "btn_wipe_10s", "wipe_button")

    def __init__(
        self,
        bus: EventBus,
        config_path: str | Path | None = None,
        poweroff_func: Callable[[], Any] | None = None,
        dry_run: bool = False,
        wipe_journal: bool = True,
    ) -> None:
        self.bus = bus
        from takpi_common.config_manager import get_default_config_path

        self.config_path = (
            Path(config_path).expanduser() if config_path else get_default_config_path()
        )
        self.poweroff_func = poweroff_func or self._default_poweroff
        self.dry_run = dry_run
        self.wipe_journal = wipe_journal
        self._press_task: asyncio.Task[None] | None = None
        self._press_start: float | None = None
        self._unsub: Any | None = None
        self._running = False
        self._gpio_task: asyncio.Task[None] | None = None
        self._gpio_header_pin: int | None = None
        self._gpio_bcm: int | None = None

    async def start(self) -> None:
        """Start listening for wipe button (MCP via EventBus + GPIO header poll)."""
        if self._running:
            return
        self._running = True
        self._unsub = self.bus.subscribe(ButtonEvent, self._on_button)
        # Check for GPIO header wipe button in config (recommended)
        try:
            from takpi_common.config_manager import load_yaml_config, header_to_bcm

            cfg = load_yaml_config(self.config_path)
            # Look for gpio wipe pin
            for a in cfg.hardware.gpio:
                if a.keyword in self.BUTTON_IDS:
                    self._gpio_header_pin = a.header_pin
                    self._gpio_bcm = a.bcm
                    logger.info(
                        "WipeManager: found GPIO wipe on header %d (BCM %d)",
                        a.header_pin,
                        a.bcm,
                    )
                    # Start GPIO poll task (header pin, preferred)
                    self._gpio_task = asyncio.create_task(
                        self._gpio_poll_loop(a.header_pin, a.bcm)
                    )
                    break
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("WipeManager GPIO check failed: %s", exc)
        # Wipe available LED – on if button defined (header preferred)
        try:
            await self._set_wipe_led("led_wipe_avail", "LED_WIPE_AVAIL", state=True)
            await self._set_wipe_led("LED_WIPE_AVAIL", "LED_WIPE_AVAIL", state=True)
        except Exception:
            pass
        logger.info(
            "WipeManager started (button %s, hold %.0fs, config %s)",
            self.BUTTON_IDS,
            self.WIPE_HOLD_S,
            self.config_path,
        )

    async def stop(self) -> None:
        """Stop listening and cancel hold timer."""
        self._running = False
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._press_task:
            self._press_task.cancel()
            try:
                await self._press_task
            except asyncio.CancelledError:
                pass
            self._press_task = None
        if self._gpio_task:
            self._gpio_task.cancel()
            try:
                await self._gpio_task
            except asyncio.CancelledError:
                pass
            self._gpio_task = None
        # Turn off wipe LEDs
        try:
            await self._set_wipe_led("led_wipe_avail", "LED_WIPE_AVAIL", state=False)
            await self._set_wipe_led("led_wipe_trig", "LED_WIPE_TRIG", state=False)
        except Exception:
            pass

    async def __aenter__(self) -> WipeManager:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stop()

    async def _set_wipe_led(
        self, *keywords: str, state: bool, blink_ms: int | None = None
    ) -> None:
        """Set wipe LEDs (LED_WIPE_AVAIL / LED_WIPE_TRIG) if configured."""
        try:
            from takpi_common.config_manager import load_yaml_config
            from takpi_common.mcp23017.io import LedCommand  # type: ignore

            cfg = load_yaml_config(self.config_path)
            for kw in keywords:
                # Try MCP first, then GPIO
                mcp_pin = None
                gpio_pin = None
                for ma in cfg.hardware.mcp:
                    if ma.keyword == kw:
                        mcp_pin = ma
                        break
                for ga in cfg.hardware.gpio:
                    if ga.keyword == kw:
                        gpio_pin = ga
                        break
                # Prefer MCP for LedCommand (HardwareManager handles it)
                target = mcp_pin or gpio_pin
                if target is not None:
                    if mcp_pin is not None:
                        await self.bus.publish(
                            LedCommand(
                                id=kw.lower(),
                                device_addr=mcp_pin.address,
                                pin=mcp_pin.pin_index,
                                state=state,
                                blink_ms=blink_ms,
                            )
                        )
                    elif gpio_pin is not None:
                        await self.bus.publish(
                            LedCommand(
                                id=kw.lower(),
                                device_addr=0,
                                pin=gpio_pin.header_pin,
                                state=state,
                                blink_ms=blink_ms,
                            )
                        )
                    logger.debug("Wipe LED %s -> %s", kw, "on" if state else "off")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("Wipe LED failed: %s", exc)

    async def _on_button(self, evt: ButtonEvent) -> None:
        if evt.id not in self.BUTTON_IDS:
            return
        if evt.pressed:
            # Button pressed – start hold timer
            if self._press_task and not self._press_task.done():
                return  # already timing
            self._press_start = time.monotonic()
            logger.info(
                "Wipe button %s pressed, hold %.0fs to wipe", evt.id, self.WIPE_HOLD_S
            )
            self._press_task = asyncio.create_task(self._hold_and_wipe(evt.id))
        else:
            # Button released – cancel if hold not yet completed
            if self._press_task and not self._press_task.done():
                elapsed = time.monotonic() - (self._press_start or 0)
                self._press_task.cancel()
                try:
                    await self._press_task
                except asyncio.CancelledError:
                    pass
                logger.info(
                    "Wipe button %s released after %.1fs – cancel wipe", evt.id, elapsed
                )
            self._press_start = None
            self._press_task = None

    async def _gpio_poll_loop(self, header_pin: int, bcm: int) -> None:
        """Poll GPIO header pin for wipe (active-low, pullup). Preferred over MCP."""
        # Try RPi.GPIO, else gpiozero, else sysfs fallback
        use_rpi = False
        use_gpiozero = False
        try:
            from RPi import GPIO  # type: ignore

            GPIO.setmode(GPIO.BOARD)
            GPIO.setup(header_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            use_rpi = True
            logger.info(
                "WipeManager: GPIO header %d monitoring via RPi.GPIO", header_pin
            )
        except (ImportError, RuntimeError, OSError):
            try:
                from gpiozero import Button as GpioZeroButton  # type: ignore

                use_gpiozero = True
                logger.info(
                    "WipeManager: GPIO header %d monitoring via gpiozero", header_pin
                )
            except (ImportError, OSError):
                logger.warning(
                    "WipeManager: no RPi.GPIO/gpiozero, GPIO wipe poll disabled for header %d",
                    header_pin,
                )
                return

        # Simple poll loop – publish ButtonEvent for wipe
        pressed = False
        while self._running:
            try:
                if use_rpi:
                    # RPi.GPIO BOARD mode: header pin number directly
                    from RPi import GPIO  # type: ignore

                    level = GPIO.input(header_pin)
                    # Active-low: 0 = pressed
                    is_pressed = level == 0
                elif use_gpiozero:
                    from gpiozero import Button as GpioZeroButton  # type: ignore

                    # gpiozero uses BCM by default, need to create button each loop? Simpler poll via value
                    # For now, fallback to sysfs
                    is_pressed = False
                else:
                    is_pressed = False

                # Fallback sysfs if needed
                if not use_rpi and not use_gpiozero:
                    # Try /sys/class/gpio
                    try:
                        val = (
                            Path(f"/sys/class/gpio/gpio{bcm}/value")
                            .read_text(encoding="utf-8")
                            .strip()
                        )
                        is_pressed = val == "0"
                    except (OSError, FileNotFoundError):
                        is_pressed = False

                if is_pressed != pressed:
                    pressed = is_pressed
                    # Publish ButtonEvent for wipe
                    evt = ButtonEvent(
                        id="wipe", device_addr=0, pin=header_pin, pressed=pressed
                    )
                    await self.bus.publish(evt)
                    # Also directly handle for immediate hold logic (in case no other handler)
                    await self._on_button(evt)
                await asyncio.sleep(0.05)  # 20Hz poll
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.debug("GPIO wipe poll error header %d: %s", header_pin, exc)
                await asyncio.sleep(1.0)

    async def _hold_and_wipe(self, button_id: str) -> None:
        try:
            await asyncio.sleep(self.WIPE_HOLD_S)
            logger.warning(
                "Wipe button %s held for %.0fs – wiping!", button_id, self.WIPE_HOLD_S
            )
            await self._execute_wipe()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("Wipe hold error: %s", exc)

    async def _execute_wipe(self) -> None:
        """Execute wipe of all registered sensitive data, then poweroff."""
        # Wipe triggered LED – on during wipe
        try:
            await self._set_wipe_led("led_wipe_trig", "LED_WIPE_TRIG", state=True)
        except Exception:
            pass
        logger.warning("WIPE START – wiping sensitive data")
        if self.dry_run:
            logger.info("WIPE dry_run – would wipe but skipping actual delete/poweroff")
            # Still log what would be wiped
            paths = WipeRegistry.get_paths()
            keys = WipeRegistry.get_config_keys()
            logger.info("WIPE dry_run paths: %s", [str(p.path) for p in paths])
            logger.info("WIPE dry_run config keys: %s", keys)
            logger.info("WIPE dry_run config file: %s", self.config_path)
            return

        # 1. Wipe registered paths (certs etc.)
        total_files = 0
        for sp in WipeRegistry.get_paths():
            # Handle globs and dirs
            pat = sp.path
            # Use _wipe_path which handles globs and absolute/relative
            # Try base dir as takpi common and cwd and home
            for base in [
                Path.cwd(),
                Path.home(),
                Path.home() / "takpi",
                Path("/home/sgofferj/takpi"),
                Path("/home/pi/takpi"),
            ]:
                # _wipe_path already searches multiple bases, so just call once with cwd
                pass
            count = _wipe_path(pat, base_dir=Path.cwd())
            total_files += count
            # Also try absolute home takpi
            if not pat.is_absolute():
                for base in [
                    Path.home() / "takpi",
                    Path("/home/sgofferj/takpi"),
                    Path("/home/pi/takpi"),
                    Path("/home/sgofferj/Dev/TAK/takpi"),
                ]:
                    count2 = _wipe_path(pat, base_dir=base)
                    total_files += count2

        # Also wipe common cache/temp patterns for our modules (only ours)
        our_caches = [
            Path("__pycache__"),
            Path(".pytest_cache"),
            Path(".mypy_cache"),
            Path(".ruff_cache"),
            Path("/tmp/tak-chronometer-healthy"),
            Path("/tmp/tak-escpos-healthy"),
            Path("/tmp/takpi"),
        ]
        for p in our_caches:
            # Search in common locations
            for base in [
                Path.cwd(),
                Path.home() / "takpi",
                Path("/home/sgofferj/Dev/TAK/takpi"),
            ]:
                target = base / p if not p.is_absolute() else p
                if target.exists():
                    if target.is_dir():
                        total_files += _wipe_dir(target)
                    elif target.is_file():
                        if _secure_overwrite_file(target):
                            total_files += 1
            # Also try absolute /tmp
            if p.is_absolute() and p.exists():
                if p.is_dir():
                    total_files += _wipe_dir(p)
                else:
                    if _secure_overwrite_file(p):
                        total_files += 1

        # Also wipe registered config keys from central config
        keys = WipeRegistry.get_config_keys()
        wiped_keys = _wipe_config_keys(self.config_path, keys)
        logger.info(
            "WIPE wiped %d files/dirs and %d config keys", total_files, wiped_keys
        )

        # 2. Wipe journal if requested
        if self.wipe_journal:
            await self._wipe_journal()

        # 3. Poweroff immediately
        logger.warning("WIPE complete – powering off")
        try:
            result = self.poweroff_func()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("Poweroff failed: %s", exc)
            # Fallback try direct
            try:
                subprocess.run(
                    ["sudo", "poweroff"], check=False, timeout=5
                )  # nosec B603,B607
            except Exception:
                pass

    async def _wipe_journal(self) -> None:
        """Wipe systemd journal (RAMdisk) if available."""
        try:
            # Rotate and vacuum
            for cmd in [["journalctl", "--rotate"], ["journalctl", "--vacuum-time=1s"]]:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                        logger.info("Wipe journal: %s done", " ".join(cmd))
                    except asyncio.TimeoutError:
                        proc.kill()
                except (OSError, FileNotFoundError):
                    logger.debug("Journalctl not available for %s", " ".join(cmd))
            # Also try to wipe journal files directly (if on disk)
            for jpath in [Path("/run/log/journal"), Path("/var/log/journal")]:
                if jpath.is_dir():
                    _wipe_dir(jpath)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.debug("Wipe journal failed: %s", exc)

    def _default_poweroff(self) -> None:
        """Default poweroff – immediate."""
        logger.warning("Executing poweroff")
        try:
            subprocess.run(
                ["sudo", "poweroff"], check=False, timeout=5
            )  # nosec B603,B607
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("poweroff failed: %s", exc)
            try:
                subprocess.run(
                    ["sudo", "systemctl", "poweroff"], check=False, timeout=5
                )  # nosec B603,B607
            except Exception:
                pass
