"""Health file helper – shared across bridges (same pattern as chronometer)."""

from __future__ import annotations

import os


def report_health(health_file: str, ok: bool) -> None:
    """Write/remove health flag. Absence = healthy (per AGENTS.md)."""
    if ok:
        try:
            os.unlink(health_file)
        except FileNotFoundError:
            pass
    else:
        with open(health_file, "w", encoding="utf-8") as f:
            f.write("unhealthy")
