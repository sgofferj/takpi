"""Shared dotenv helper – thin wrapper around python-dotenv."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(env_path: str | Path | None = None) -> None:
    """Load ``.env`` if present (no-op if missing). Wraps python-dotenv."""
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]

        load_dotenv(dotenv_path=env_path)
    except ImportError:
        # dotenv not installed – env vars still read from process
        pass


def get_str(name: str, default: str = "") -> str:
    """Return env var ``name`` as string, or ``default``."""
    return os.getenv(name, default)


def get_int(name: str, default: int = 0) -> int:
    """Return env var ``name`` as int, or ``default`` on error."""
    try:
        return int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


def get_float(name: str, default: float = 0.0) -> float:
    """Return env var ``name`` as float, or ``default`` on error."""
    try:
        return float(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


def get_bool(name: str, default: bool = False) -> bool:
    """Return env var ``name`` as bool (1/true/yes/on → True)."""
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")
