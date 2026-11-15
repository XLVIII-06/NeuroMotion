"""Stable writable paths for both source and the frozen Windows desktop app."""

from pathlib import Path
import sys


def app_root() -> Path:
    """Keep experiments next to the launched EXE, outside its temporary bundle."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_path(name: str) -> Path:
    """Prefer a local editable resource, then the read-only bundled default."""
    local = app_root() / name
    if local.is_file():
        return local
    return Path(__file__).resolve().parents[1] / name
