"""
EventBus – central async pub/sub for takpi.

Bridges hardware (MCP23017 buttons/encoders → LED commands) and application
logic (CoT → LED, button → CoT). All participants share a single bus instance
created by the main process.

Design:
  - Typed events (dataclasses) – ``ButtonEvent``, ``EncoderEvent``, ``LedCommand``,
    ``CotReceived`` / ``CotSend`` are defined here or by submodules but bus is generic.
  - ``subscribe(event_type, handler)`` – handler is ``async def(event)`` or sync.
  - ``publish(event)`` – dispatches to exact type + base ``object`` wildcard.
  - Errors in one handler never break others (logged).

Example bidirectional flow::

    bus = EventBus()
    # hardware → main
    bus.subscribe(ButtonEvent, lambda e: handle_button(e))
    # main → hardware (LED)
    await bus.publish(LedCommand(id="alert_led", state=True))
    # CoT → LED
    bus.subscribe(CotReceived, lambda e: bus.publish(LedCommand(...)) if e.cot.is_emergency() else None)

Testable without hardware: bus is pure asyncio, no I2C/GPIO.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


Handler = Callable[[Any], Any | Awaitable[Any]]


class EventBus:
    """Async pub/sub bus for takpi main process ↔ hardware ↔ CoT."""

    def __init__(self) -> None:
        """__init__."""
        self._subs: dict[Type[Any], list[Handler]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def subscribe(
        self, event_type: Type[T], handler: Callable[[T], Any | Awaitable[Any]]
    ) -> Callable[[], None]:
        """
        Subscribe ``handler`` to ``event_type``.

        Returns an ``unsubscribe()`` callable. Use ``object`` to subscribe to all
        events (wildcard).
        """
        self._subs[event_type].append(handler)  # type: ignore[arg-type]

        def unsubscribe() -> None:
            """unsubscribe."""
            try:
                self._subs[event_type].remove(handler)  # type: ignore[arg-type]
            except ValueError:
                pass

        return unsubscribe

    def subscribed(self, event_type: Type[Any]) -> int:
        """Number of handlers subscribed to ``event_type`` (for tests/metrics)."""
        return len(self._subs.get(event_type, []))

    async def publish(self, event: Any) -> None:
        """Publish ``event`` to all matching subscribers (exact type + wildcard)."""
        handlers: list[Handler] = []
        # Exact type first, then wildcard ``object`` subscribers
        handlers.extend(self._subs.get(type(event), []))
        if type(event) is not object:
            handlers.extend(self._subs.get(object, []))  # type: ignore[arg-type]

        if not handlers:
            logger.debug("EventBus: no subscribers for %s", type(event).__name__)
            return

        for handler in list(handlers):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.exception(
                    "EventBus handler %s failed for %s: %s",
                    handler,
                    type(event).__name__,
                    exc,
                )

    async def wait_for(self, event_type: Type[T], timeout: float | None = None) -> T:
        """
        Wait for the next event of ``event_type``.

        Useful in tests: ``await bus.wait_for(ButtonEvent, timeout=1.0)``.
        """
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()

        def _handler(event: T) -> None:
            """_handler."""
            if not future.done():
                future.set_result(event)

        unsub = self.subscribe(event_type, _handler)  # type: ignore[arg-type]
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            unsub()
