"""
Main entry for tak-bridge-escpos – async ESC/POS printer service (softserial GPIO).

Listens on EventBus for PrintRequest and CotReceived (911 emergency) and prints
via EscPosPrinter (hardware serial /dev/serial0 or softserial BCM pins).

Env (via .env):
  ESCPOS_PORT         Hardware port (/dev/serial0, /dev/ttyUSB0) – mutually exclusive with TX_PIN
  ESCPOS_BAUDRATE     9600|19200|38400|57600|115200 (default 9600, printer DIP)
  ESCPOS_TX_PIN       BCM TX for softserial (e.g. 27) – Pi header pin -> printer RX
  ESCPOS_RX_PIN       BCM RX for softserial (optional, e.g. 22) – printer TX → Pi
  ESCPOS_ENCODING     cp437 (default) or utf8
  TAK_HOST/PORT/CERT/KEY/CALLSIGN/TEAM/ROLE  – optional TAK streaming for 911 auto-print
  HEALTH_FILE         /tmp/tak-escpos-healthy
  DEBUG               1 for DEBUG
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

from dotenv import load_dotenv

from takpi_common.bus import EventBus
from takpi_common.health import report_health
from tak_bridge_escpos.escpos import EscPosPrinter, FakeSerial, PrintRequest

logger = logging.getLogger(__name__)

HEALTH_FILE: str = os.getenv("HEALTH_FILE", "/tmp/tak-escpos-healthy")
HEALTH_MAX_ERRORS: int = int(os.getenv("HEALTH_MAX_ERRORS", "3"))


def _report(ok: bool) -> None:
    report_health(HEALTH_FILE, ok)


def _printer_from_env() -> EscPosPrinter:
    # Try central hardware config first (header pins → BCM, per spec)
    # Transparently falls back to env if not configured
    try:
        from takpi_common.config_manager import header_to_bcm, load_yaml_config

        cfg = load_yaml_config()
        # Check hardware.gpio for escpos_tx/rx keywords (header numbers)
        tx_header = None
        rx_header = None
        for assignment in cfg.hardware.gpio:
            if assignment.keyword == "escpos_tx":
                tx_header = assignment.header_pin
            elif assignment.keyword == "escpos_rx":
                rx_header = assignment.header_pin
        tx_from_cfg = header_to_bcm(tx_header) if tx_header is not None else None
        rx_from_cfg = header_to_bcm(rx_header) if rx_header is not None else None
        if tx_from_cfg is not None:
            baud = int(
                os.getenv("ESCPOS_BAUDRATE", os.getenv("PRINTER_BAUDRATE", "9600"))
            )
            encoding = os.getenv("ESCPOS_ENCODING", "cp437")
            logger.info(
                "ESCPOS softserial from config.yaml header TX %d→BCM %d RX %s @%d",
                tx_header,
                tx_from_cfg,
                str(rx_from_cfg),
                baud,
            )
            return EscPosPrinter(
                tx_pin=tx_from_cfg, rx_pin=rx_from_cfg, baudrate=baud, encoding=encoding
            )
        if tx_from_cfg is None and rx_from_cfg is not None:
            logger.warning("Config has escpos_rx without escpos_tx, ignoring")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug("Hardware config escpos lookup failed (fallback to env): %s", exc)

    port = os.getenv("ESCPOS_PORT") or os.getenv("PRINTER_PORT")
    baud = int(os.getenv("ESCPOS_BAUDRATE", os.getenv("PRINTER_BAUDRATE", "9600")))
    tx_raw = os.getenv("ESCPOS_TX_PIN") or os.getenv("PRINTER_TX_PIN")
    rx_raw = os.getenv("ESCPOS_RX_PIN") or os.getenv("PRINTER_RX_PIN")
    encoding = os.getenv("ESCPOS_ENCODING", "cp437")
    tx_pin = int(tx_raw) if tx_raw and tx_raw.strip() else None
    rx_pin = int(rx_raw) if rx_raw and rx_raw.strip() else None

    if tx_pin is not None:
        logger.info(
            "ESCPOS softserial BCM TX=%s RX=%s @%d", str(tx_pin), str(rx_pin), baud
        )
        return EscPosPrinter(
            tx_pin=tx_pin, rx_pin=rx_pin, baudrate=baud, encoding=encoding
        )
    if port:
        logger.info("ESCPOS hardware serial %s @%d", port, baud)
        return EscPosPrinter(port=port, baudrate=baud, encoding=encoding)
    # Default for bench / tests without hardware – FakeSerial injected
    logger.warning(
        "No ESCPOS_PORT or ESCPOS_TX_PIN set – using default /dev/serial0 @9600 (may fail without hardware)"
    )
    return EscPosPrinter(port="/dev/serial0", baudrate=baud, encoding=encoding)


async def _handle_print_request(printer: EscPosPrinter, req: PrintRequest) -> None:
    try:
        await printer.handle_print_request(req)
        _report(True)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("PrintRequest failed: %s", exc)
        _report(False)


def _format_location(cot: object) -> str:
    lat = getattr(cot, "lat", None)
    lon = getattr(cot, "lon", None)
    if (
        isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and lat != 9999999
        and lon != 9999999
    ):
        return f"{lat:.5f}, {lon:.5f}"
    return "unknown"


async def _handle_cot_print(bus: EventBus, printer: EscPosPrinter, cot: object) -> None:
    """
    911 alarm printer – called for each CotReceived.
    Main process helper: prints "911 alarm received from <callsign> at <location>".
    """
    # Use takstream predicates if available
    is_emergency = False
    if hasattr(cot, "is_emergency"):
        try:
            is_emergency = cot.is_emergency()  # type: ignore[call-arg]
        except Exception:
            pass
    else:
        ctype = str(getattr(cot, "cot_type", ""))
        is_emergency = ctype.startswith("b-a-o-tif") or ctype.startswith("b-a-o-can")

    if not is_emergency:
        return

    callsign = getattr(cot, "callsign", None) or getattr(cot, "uid", "unknown")
    location = _format_location(cot)
    remarks = getattr(cot, "remarks", None)
    cot_type = getattr(cot, "cot_type", None)
    ts = getattr(cot, "time", None) or getattr(cot, "start", None)
    if not isinstance(ts, datetime):
        ts = datetime.now(timezone.utc)

    logger.info("911 alarm from %s at %s – printing", callsign, location)
    try:
        await printer.print_alarm(
            callsign=str(callsign),
            lat=(
                getattr(cot, "lat", None)
                if isinstance(getattr(cot, "lat", None), float)
                else None
            ),
            lon=(
                getattr(cot, "lon", None)
                if isinstance(getattr(cot, "lon", None), float)
                else None
            ),
            location=location,
            when=ts,
            remarks=remarks,
            cot_type=cot_type,
        )
        await printer.cut()
        _report(True)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("911 print failed: %s", exc)
        _report(False)


async def main() -> None:  # pylint: disable=too-many-statements
    load_dotenv()
    global HEALTH_FILE
    HEALTH_FILE = os.getenv("HEALTH_FILE", HEALTH_FILE)
    log_level = logging.DEBUG if os.getenv("DEBUG", "0") == "1" else logging.INFO
    logging.basicConfig(
        level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    bus = EventBus()
    printer = _printer_from_env()

    # For tests without hardware: if no real port and no pigpio, inject FakeSerial via env ESCPOS_FAKE=1
    if os.getenv("ESCPOS_FAKE", "0") == "1":
        # Override printer to use FakeSerial
        printer = EscPosPrinter(serial_instance=FakeSerial(), encoding="cp437")
        logger.info("ESCPOS_FAKE=1 – using FakeSerial (no hardware)")

    try:
        await printer.connect()
        _report(True)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error(
            "Printer connect failed on %s/%s @%d: %s",
            printer.port,
            str(printer.tx_pin),
            printer.baudrate,
            exc,
        )
        _report(False)
        # Continue – let reconnection logic handle? For now exit if no fake
        if os.getenv("ESCPOS_FAKE") != "1":
            # Keep running to allow EventBus handlers to report health, but warn
            pass

    # Subscribe PrintRequest → printer (main → printer)
    async def on_print(req: PrintRequest) -> None:
        await _handle_print_request(printer, req)

    bus.subscribe(PrintRequest, on_print)
    logger.info(
        "Subscribed PrintRequest → printer (softserial=%s)", printer.is_softserial
    )

    # Optionally connect CotStream for 911 auto-print
    cot_bus = None
    stream = None
    tak_host = os.getenv("TAK_HOST") or os.getenv("API_HOST")
    tak_port = os.getenv("TAK_PORT") or os.getenv("API_PORT")
    if tak_host and tak_port:
        try:
            from takpi_common.cot_bus import CotBus, CotReceived  # type: ignore[import-not-found]
            from takstream import CotStream  # type: ignore[import-not-found]

            host = tak_host
            port = int(tak_port)
            cert = os.getenv("TAK_CERT") or os.getenv("CLIENT_CERT")
            key = os.getenv("TAK_KEY") or os.getenv("CLIENT_KEY")
            ca = os.getenv("TAK_CA")
            callsign = os.getenv("TAK_CALLSIGN") or os.getenv("MY_UID") or "PI-PRINTER"
            team = os.getenv("TAK_TEAM", "Cyan")
            role = os.getenv("TAK_ROLE", "Team Member")

            logger.info("Connecting CotStream %s:%d for 911 auto-print", host, port)
            stream = await CotStream.connect(
                host,
                port,
                cert=cert,
                key=key,
                ca=ca,
                callsign=callsign,
                team=team,
                role=role,
            )
            cot_bus = CotBus(bus, stream)
            await cot_bus.start()

            async def on_cot(evt: CotReceived) -> None:  # type: ignore[no-untyped-def]
                await _handle_cot_print(bus, printer, evt.cot)

            bus.subscribe(CotReceived, on_cot)
            logger.info("CoT 911 auto-print subscribed (CotReceived → printer)")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "CoT streaming disabled (no takstream or connect failed): %s", exc
            )

    # Graceful shutdown
    stop_event = asyncio.Event()

    def _signal(*_: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal)
        except NotImplementedError:
            pass

    logger.info(
        "tak-bridge-escpos ready – publish PrintRequest on EventBus or send 911 CoT to print"
    )
    # Example: main could publish PrintRequest directly:
    # await bus.publish(PrintRequest(text="Hello", fat=True, cut_after=True))

    # Keep alive until signal, tick health
    consecutive_errors = 0
    while not stop_event.is_set():
        await asyncio.sleep(1.0)
        # Health already managed per print; could add periodic check
        if consecutive_errors >= HEALTH_MAX_ERRORS:
            _report(False)

    logger.info("Shutting down printer bridge")
    if cot_bus:
        await cot_bus.stop()
    if stream:
        try:
            await stream.close()  # type: ignore[union-attr]
        except Exception:
            pass
    await printer.disconnect()
    _report(True)


def run() -> None:
    """Sync entry for poetry ``start``."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
