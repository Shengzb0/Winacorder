"""Development entry point for WinActionRecorder."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from win_action_recorder.app import main

if __name__ == "__main__":
    raise SystemExit(main())
