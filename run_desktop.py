#!/usr/bin/env python3
"""EGO Courier 桌面版启动器：python run_desktop.py（或双击本文件）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ego_gui.main import main

if __name__ == "__main__":
    sys.exit(main())
