"""
TakPiApp – example main process wiring EventBus ↔ MCP23017 hardware ↔ CotStream.

This is the reference integration showing bidirectional flow:

  hardware (button/encoder) ──EventBus──► main logic ──CotSend──► CotStream
  CotStream ──CotReceived──► EventBus ──► main logic ──LedCommand──► hardware

It is intentionally minimal and meant to be copied/adapted per deployment.
All hardware I/O is mocked without Pi via FakeSMBus; CoT streaming can be
tested against a local takserver or with a fake stream.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from takpi_common.bus import EventBus
from takpi_common.cot_bus import CotBus, CotReceived, CotSend
from takpi_common.mcp23017.io import ButtonEvent, EncoderEvent, LedCommand
from takpi_common.mcp23017.manager import HardwareConfig, HardwareManager

logger = logging.getLogger(__name__)


@dataclass
class CotConfig:
    host: str
    port: int
    cert: str | None = None
    key: str | None = None
    ca: str | None = None
    callsign: str | None = None
    team: str | None = None
    role: str | None = None
    cot_type: str = "a-f-G-U-C"
    uid: str | None = None


class TakPiApp:
    """
    Example main process for takpi.

    - Owns a single EventBus (shared)
    - Starts HardwareManager for MCP23017 chain
    - Optionally connects CotStream via CotBus
    - Registers example handlers: button→CoT, CoT→LED, encoder→LED/CoT

    For real deployments, replace ``_on_button``, ``_on_encoder``, ``_on_cot``
    with your own business logic. The bus stays the integration point.
    """

    def __init__(
        self,
        hardware_cfg: HardwareConfig,
        cot_cfg: CotConfig | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.bus = bus or EventBus()
        self.hardware_cfg = hardware_cfg
        self.cot_cfg = cot_cfg
        self.hardware: HardwareManager | None = None
        self.cot_bus: CotBus | None = None
        self._stream: Any | None = None
        self._subs: list[Any] = []

    async def start(self) -> None:
        # Hardware
        self.hardware = HardwareManager(self.hardware_cfg, self.bus)
        await self.hardware.start()

        # Subscribe hardware → main handlers
        self._subs.append(self.bus.subscribe(ButtonEvent, self._on_button))
        self._subs.append(self.bus.subscribe(EncoderEvent, self._on_encoder))
        # Cot → LED handler is always active (even without live CotStream, allows synthetic CotReceived in tests)
        self._subs.append(self.bus.subscribe(CotReceived, self._on_cot))

        # CoT streaming (optional)
        if self.cot_cfg:
            await self._connect_cot(self.cot_cfg)

        logger.info(
            "TakPiApp started (hardware=%s, cot=%s)",
            bool(self.hardware),
            bool(self.cot_cfg),
        )

    async def stop(self) -> None:
        for unsub in self._subs:
            try:
                unsub()
            except Exception:
                pass
        self._subs.clear()
        if self.cot_bus:
            await self.cot_bus.stop()
            self.cot_bus = None
        if self._stream:
            try:
                await self._stream.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._stream = None
        if self.hardware:
            await self.hardware.stop()
            self.hardware = None
        logger.info("TakPiApp stopped")

    async def __aenter__(self) -> TakPiApp:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stop()

    # -- CoT connection --------------------------------------------------------

    async def _connect_cot(self, cfg: CotConfig) -> None:
        # Lazy import to keep app importable without takstream installed (tests)
        try:
            from takstream import CotStream  # type: ignore
        except ImportError as exc:
            logger.warning("takstream not available, CotBus disabled: %s", exc)
            return

        logger.info(
            "Connecting CotStream to %s:%d (callsign=%s)",
            cfg.host,
            cfg.port,
            cfg.callsign,
        )
        stream = await CotStream.connect(
            cfg.host,
            cfg.port,
            cert=cfg.cert,
            key=cfg.key,
            ca=cfg.ca,
            callsign=cfg.callsign,
            team=cfg.team,
            role=cfg.role,
            cot_type=cfg.cot_type,
            uid=cfg.uid,
        )
        self._stream = stream
        self.cot_bus = CotBus(self.bus, stream)
        await self.cot_bus.start()

    # -- Example handlers (replace with your logic) ----------------------------

    async def _on_button(self, evt: ButtonEvent) -> None:
        logger.info("Button %s %s", evt.id, "pressed" if evt.pressed else "released")
        # Example: button → CoT (emergency) and LED toggle
        if evt.id == "btn_emergency" and evt.pressed:
            try:
                from takstream import CotEvent  # type: ignore

                cot = CotEvent.emergency_alert(lat=60.0, lon=24.0, callsign="PI-PANEL")
                await self.bus.publish(CotSend(cot=cot))
                # Visual ack
                await self.bus.publish(
                    LedCommand(
                        id="led_alert", device_addr=evt.device_addr, pin=0, state=True
                    )
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.exception("button→cot failed: %s", exc)
        # Generic: toggle LED on any press (if led id matches button id)
        else:
            # Example: btn1 → led1
            led_id = evt.id.replace("btn", "led")
            await self.bus.publish(
                LedCommand(
                    id=led_id,
                    device_addr=evt.device_addr,
                    pin=evt.pin,
                    state=evt.pressed,
                )
            )

    async def _on_encoder(self, evt: EncoderEvent) -> None:
        logger.info("Encoder %s delta %d pos %d", evt.id, evt.delta, evt.position)
        # Example: encoder → CoT position update or LED brightness (not implemented)
        # Encoder clockwise → LED on, counter → LED off
        led_state = evt.delta > 0
        await self.bus.publish(
            LedCommand(
                id=f"led_{evt.id}", device_addr=evt.device_addr, pin=0, state=led_state
            )
        )

    async def _on_cot(self, evt: CotReceived) -> None:
        cot = evt.cot
        # Example: specific CoT → LED
        # Use takstream.cot.CotEvent predicates
        try:
            cot_type = getattr(cot, "cot_type", "")
            callsign = getattr(cot, "callsign", None) or getattr(cot, "uid", "?")
            logger.debug("CoT received %s %s", cot_type, callsign)

            # Emergency CoT → alert LED blink
            is_emergency = False
            if hasattr(cot, "is_emergency"):
                is_emergency = cot.is_emergency()  # type: ignore[call-arg]
            elif "b-a-o-tif" in str(cot_type) or "b-a-o-can" in str(cot_type):
                is_emergency = True

            if is_emergency:
                await self.bus.publish(
                    LedCommand(
                        id="led_alert",
                        device_addr=0x20,
                        pin=0,
                        state=True,
                        blink_ms=500,
                    )
                )
            # Example: GeoChat → chat LED
            is_chat = hasattr(cot, "is_chat") and cot.is_chat()  # type: ignore[call-arg]
            if is_chat:
                await self.bus.publish(
                    LedCommand(id="led_chat", device_addr=0x20, pin=1, state=True)
                )
            # Example: team Cyan → team LED
            team = getattr(cot, "team", None)
            if team == "Cyan":
                await self.bus.publish(
                    LedCommand(id="led_cyan", device_addr=0x21, pin=0, state=True)
                )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("cot→led failed: %s", exc)
