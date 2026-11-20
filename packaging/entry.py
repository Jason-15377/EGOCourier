#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EGO Courier 打包入口（专供 PyInstaller 使用）。

与 ``run_desktop.py`` 的区别：冻结后的程序运行在 ``sys._MEIPASS`` 临时解压目录，
脚本自身所在目录不再是项目根，因此这里显式把项目根（源码模式）与 ``_MEIPASS``
（冻结模式）都加进 ``sys.path``，保证 ``ego_gui`` / ``ego_relay`` 都能被导入。
"""

from __future__ import annotations

import os
import sys

if getattr(sys, "frozen", False):
    # PyInstaller onefile / onedir：资源与包都在 _MEIPASS 下
    _root = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
else:
    # 源码模式：packaging/ 的上一级
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if _root not in sys.path:
    sys.path.insert(0, _root)


def _run() -> int:
    # 冻结后的分析子进程会带着 --multiprocessing-fork 重新执行本 exe；这里
    # 必须先分发，且不能先导入 Qt/ego_gui，否则子进程会再拉起一个主窗口。
    import multiprocessing

    multiprocessing.freeze_support()

    from ego_gui.main import main as gui_main  # noqa: E402

    return gui_main()


if __name__ == "__main__":
    sys.exit(_run())
