"""Qt application bootstrap."""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from . import __version__
from .branding import application_icon
from .logging_utils import configure_logging
from .screen import enable_per_monitor_dpi_awareness
from .ui.main_window import MainWindow
from .ui.styles import LIGHT_STYLESHEET

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Create and run the desktop application."""
    configure_logging()
    enable_per_monitor_dpi_awareness()
    QCoreApplication.setOrganizationName("WinActionRecorder")
    QCoreApplication.setApplicationName("WinActionRecorder")
    QCoreApplication.setApplicationVersion(__version__)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationDisplayName("WinActionRecorder")
    icon = application_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    app.setStyle("Fusion")
    app.setStyleSheet(LIGHT_STYLESHEET)

    try:
        window = MainWindow()
        window.show()
        return app.exec()
    except Exception as exc:  # pragma: no cover - final GUI safety net
        logger.exception("应用启动失败")
        QMessageBox.critical(None, "启动失败", f"应用无法启动：\n{exc}")
        return 1


__all__ = ["main"]
