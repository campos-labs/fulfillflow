"""Native-only fixture: no application, database or network."""

import subprocess
import sys
import time
from pathlib import Path

if __name__ == "__main__":
    if sys.argv[1] == "tree":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        Path(sys.argv[2]).write_text(str(child.pid))
        time.sleep(120)
    elif sys.argv[1] == "exit":
        raise SystemExit(2)
