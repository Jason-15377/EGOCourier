"""让 `python -m ego_gui` 可用（等价 `python run_desktop.py`）。"""

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
