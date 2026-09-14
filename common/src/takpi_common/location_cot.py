"""Bridge LocationUpdate → CotSend (SA report) for takpi.

Subscribes to :class:`takpi_common.location.LocationUpdate` on :class:`EventBus`
and publishes :class:`takpi_common.cot_bus.CotSend` with a ``CotEvent`` SA.
Used by main apps (e.g. ``TakPiApp`` or ``tak-bridge-chronometer``) to
announce Pi position to TAK server via ``CotBus``/``CotStream``.

Lazy imports ``takstream`` so tests without the submodule still pass (no-op).
"""

from __future__ import annotations

import logging
from typing import Any

from takpi_common.bus import EventBus
from takpi_common.cot_bus import CotSend
from takpi_common.location import LocationUpdate

logger = logging.getLogger(__name__)


class LocationCotBridge:
    """Forward LocationUpdate as TAK SA ``a-f-G-U-C``.

    Example::

        bus = EventBus()
        bridge = LocationCotBridge(bus, callsign="TAKPI-01", team="Cyan")
        await bridge.start()
        # LocationProvider will publish LocationUpdate → bridge publishes CotSend
        # CotBus will then send via CotStream
    """

    def __init__(
        self,
        bus: EventBus,
        callsign: str = "TAKPI",
        team: str = "Cyan",
        role: str = "Team Member",
        cot_type: str = "a-f-G-U-C",
        stale_s: float = 120.0,
    ) -> None:
        self.bus = bus
        self.callsign = callsign
        self.team = team
        self.role = role
        self.cot_type = cot_type
        self.stale_s = stale_s
        self._unsub: Any | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._unsub = self.bus.subscribe(LocationUpdate, self._on_location)
        logger.info(
            "LocationCotBridge started (callsign=%s team=%s)", self.callsign, self.team
        )

    async def stop(self) -> None:
        self._started = False
        if self._unsub:
            self._unsub()
            self._unsub = None
        logger.info("LocationCotBridge stopped")

    async def __aenter__(self) -> LocationCotBridge:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stop()

    async def _on_location(self, evt: LocationUpdate) -> None:
        # Build CotEvent lazily
        try:
            from takstream import CotEvent  # type: ignore
        except ImportError as exc:
            logger.debug("takstream not available, skip Location→Cot: %s", exc)
            return

        # Use SA report / marker with team/role
        # takstream API: CotEvent.marker or sa_report? Check cot.py – both exist.
        # Prefer sa_report if available for proper stale/team.
        try:
            # Try new API signature
            if hasattr(CotEvent, "sa_report"):
                # CotEvent.sa_report(lat, lon, callsign, team, role, cot_type, stale)
                # Inspect cot.py for signature – fallback to marker if fails
                try:
                    cot = CotEvent.sa_report(  # type: ignore[call-arg]
                        lat=evt.latitude,
                        lon=evt.longitude,
                        callsign=self.callsign,
                        team=self.team,
                        role=self.role,
                        cot_type=self.cot_type,
                        stale=self.stale_s,
                    )
                except TypeError:
                    # Older signature without stale
                    cot = CotEvent.sa_report(  # type: ignore[call-arg]
                        lat=evt.latitude,
                        lon=evt.longitude,
                        callsign=self.callsign,
                        team=self.team,
                        role=self.role,
                    )
            elif hasattr(CotEvent, "marker"):
                # marker(lat, lon, callsign, cot_type, stale)
                try:
                    cot = CotEvent.marker(  # type: ignore[call-arg]
                        lat=evt.latitude,
                        lon=evt.longitude,
                        callsign=self.callsign,
                        cot_type=self.cot_type,
                        stale=self.stale_s,
                    )
                    # team/role set via setters if available?
                    if hasattr(cot, "team"):
                        try:
                            cot.team = self.team  # type: ignore[attr-defined]
                        except Exception:
                            pass
                    if hasattr(cot, "role"):
                        try:
                            cot.role = self.role  # type: ignore[attr-defined]
                        except Exception:
                            pass
                except TypeError:
                    cot = CotEvent.marker(lat=evt.latitude, lon=evt.longitude, callsign=self.callsign)  # type: ignore[call-arg]
            else:
                logger.warning(
                    "CotEvent has no sa_report/marker, cannot publish location"
                )
                return
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "Failed to build CotEvent for location %.5f,%.5f: %s",
                evt.latitude,
                evt.longitude,
                exc,
            )
            return

        # Altitude if present: some takstream versions accept hae in CotEvent detail?
        # We expose via attribute if available, else ignore.
        if evt.altitude is not None and hasattr(cot, "hae"):
            try:
                cot.hae = evt.altitude  # type: ignore[attr-defined]
            except Exception:
                pass

        try:
            await self.bus.publish(CotSend(cot=cot))
            logger.info(
                "LocationCotBridge: published SA %.5f,%.5f from %s",
                evt.latitude,
                evt.longitude,
                evt.source,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception("LocationCotBridge publish failed: %s", exc)
