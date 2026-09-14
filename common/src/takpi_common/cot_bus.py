"""
CotBus – bridges takstream.CotStream ↔ takpi_common.EventBus.

Bidirectional:
  CotStream (network) ── CotReceived ──► EventBus ──► main logic
  main logic ── CotSend ──► EventBus ──► CotBus ──► CotStream.send

Main process controls LEDs based on CoTs by subscribing to ``CotReceived`` and
publishing ``LedCommand``. Buttons/encoders trigger CoTs by publishing
``CotSend`` (or directly calling ``cot_bus.send``).

Uses ``python-tak-cot-streaming`` (takstream), not pytak, via git submodule
``takpi/python-tak-cot-streaming`` (url ``../python-tak-cot-streaming``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from typing import Any

from takpi_common.bus import EventBus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CotReceived:
    """Inbound CoT from streaming server, published on bus."""

    cot: (
        object  # takstream.cot.CotEvent – use object to avoid hard import at type-check
    )
    raw_xml: str | None = None


@dataclass(frozen=True)
class CotSend:
    """Outbound CoT request, published on bus to be sent via CotStream."""

    cot: object  # CotEvent


class CotBus:
    """
    Bridge between ``takstream.CotStream`` and ``EventBus``.

    Example bidirectional wiring in main::

        bus = EventBus()
        cot_bus = CotBus(bus, stream)  # stream is takstream.CotStream
        await cot_bus.start()  # subscribes to CotSend, starts receive loop

        # CoT → LED: main subscribes to CotReceived
        bus.subscribe(CotReceived, lambda e: handle_cot(e.cot))

        # Button → CoT: main subscribes to ButtonEvent and publishes CotSend
        async def on_button(evt: ButtonEvent):
            if evt.id == "alert" and evt.pressed:
                cot = CotEvent.emergency_alert(lat=..., lon=..., callsign="PI")
                await bus.publish(CotSend(cot=cot))

        bus.subscribe(ButtonEvent, on_button)

        # Hardware → LED control via bus (implicit: HardwareManager subscribes to LedCommand)
    """

    def __init__(self, bus: EventBus, stream: object) -> None:
        self.bus = bus
        self.stream = stream  # takstream.CotStream
        self._recv_task: asyncio.Task[None] | None = None
        self._running = False
        self._unsub_send: Any = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Subscribe to outbound CoT requests (main → network)
        self._unsub_send = self.bus.subscribe(CotSend, self._handle_send)
        self._recv_task = asyncio.create_task(self._recv_loop(), name="cotbus-recv")
        logger.info("CotBus started (bridge CotStream ↔ EventBus)")

    async def stop(self) -> None:
        self._running = False
        if self._unsub_send:
            self._unsub_send()
            self._unsub_send = None
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None
        logger.info("CotBus stopped")

    async def __aenter__(self) -> CotBus:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stop()

    async def _handle_send(self, evt: CotSend) -> None:
        # Send via CotStream
        try:
            # stream.send is async
            await self.stream.send(evt.cot)  # type: ignore[attr-defined]
            logger.debug("CotBus sent %s", getattr(evt.cot, "cot_type", "?"))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("CotBus send failed: %s", exc)

    async def _recv_loop(self) -> None:
        # Adapt to takstream CotStream async iterator or receive()
        stream = self.stream
        try:
            # Prefer async iterator if available
            if hasattr(stream, "__aiter__"):
                async for cot in stream:  # type: ignore[attr-defined]
                    if not self._running:
                        break
                    # cot is CotEvent
                    await self.bus.publish(CotReceived(cot=cot, raw_xml=None))
                    logger.debug("CotBus received %s", getattr(cot, "cot_type", "?"))
            else:
                # Fallback to receive() loop
                while self._running:
                    cot = await stream.receive()  # type: ignore[attr-defined]
                    if cot is None:
                        break
                    await self.bus.publish(CotReceived(cot=cot, raw_xml=None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("CotBus recv loop error: %s", exc)

    async def send(self, cot: object) -> None:
        """Convenience: publish CotSend on bus (or direct send)."""
        await self.bus.publish(CotSend(cot=cot))
