"""Invoke only the reviewed control preparation wrapper; measured code stays isolated."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.paired_controls import main

if __name__ == "__main__":
    raise SystemExit(main())
