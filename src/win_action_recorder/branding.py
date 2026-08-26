"""Application branding assets shared by the Qt shell and packager."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from PySide6.QtGui import QIcon

ASSET_DIRECTORY: Final[Path] = Path(__file__).resolve().parent / "assets"
APP_ICON_PNG: Final[Path] = ASSET_DIRECTORY / "app_icon_512.png"
APP_ICON_ICO: Final[Path] = ASSET_DIRECTORY / "app_icon.ico"


def application_icon() -> QIcon:
    """Load the packaged application icon without changing global Qt state."""

    return QIcon(str(APP_ICON_PNG))


__all__ = [
    "APP_ICON_ICO",
    "APP_ICON_PNG",
    "ASSET_DIRECTORY",
    "application_icon",
]
