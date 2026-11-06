"""Run the current diagnostics wrapper while the control itself uses frozen v1.0 code."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.controls_v10 import main

if __name__ == "__main__":
    raise SystemExit(main())
