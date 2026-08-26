"""Privacy-conscious local logging configuration."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tempfile import gettempdir


def configure_logging(log_dir: Path | None = None) -> Path:
    """Configure a small local log that never intentionally records action data."""
    root = logging.getLogger("win_action_recorder")
    local_base = os.environ.get("LOCALAPPDATA")
    local_app_data = Path(local_base) if local_base else Path.home() / "AppData" / "Local"
    candidates = (
        [log_dir]
        if log_dir is not None
        else [
            local_app_data / "WinActionRecorder" / "logs",
            Path(gettempdir()) / "WinActionRecorder" / "logs",
        ]
    )
    for target_dir in candidates:
        log_path = target_dir / "application.log"
        if root.handlers:
            return log_path
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                log_path,
                maxBytes=512 * 1024,
                backupCount=2,
                encoding="utf-8",
            )
        except OSError:
            continue
        root.setLevel(logging.INFO)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
        return log_path

    root.addHandler(logging.NullHandler())
    return candidates[-1] / "application.log"
