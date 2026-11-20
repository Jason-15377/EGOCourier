# -*- coding: utf-8 -*-
"""打包版并发分析的子进程分发回归测试。

现场问题：v1.1.0 便携版在「分析并发」选 4 个进程后，每启动一个分析子进程
就弹出一个新的 EGO Courier 主窗口，分析停在 Part 1/N 不再往下走。

成因：Windows 上 multiprocessing 的 spawn 子进程是「重新执行同一个 exe +
--multiprocessing-fork」。冻结程序必须在创建 QApplication / 主窗口之前调用
``multiprocessing.freeze_support()``，把控制权交给 ``spawn_main``。
PyInstaller 的运行钩子只**替换** freeze_support 的实现，不会自动调用；
``packaging/entry.py``、``packaging/entry_portable.py`` 与
``ego_gui.main.main()`` 此前都没有调用，子进程于是直接走了 GUI 启动流程。

这里锁住「分发先于建窗」：freeze_support 必须是入口里第一个生效的动作，
早于路径安装、启动自检和 QApplication 创建。
"""
import importlib.util
import multiprocessing
import os
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class _Stop(Exception):
    """freeze_support 被调用时抛出的哨兵：证明分发发生在后续动作之前。"""


def _load_entry(filename):
    """按路径加载打包入口，避免和 site-packages 里的 ``packaging`` 撞名。"""
    path = ROOT / "packaging" / filename
    spec = importlib.util.spec_from_file_location(
        "entry_under_test_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestGuiMainFreezesFirst(unittest.TestCase):
    def test_freeze_support_runs_before_qapplication(self):
        from ego_gui import main as main_mod

        with mock.patch.object(multiprocessing, "freeze_support",
                               side_effect=_Stop), \
                mock.patch.object(main_mod, "QApplication",
                                  side_effect=AssertionError(
                                      "窗口在 freeze_support 之前创建")):
            with self.assertRaises(_Stop):
                main_mod.main()


class TestPortableEntryFreezesFirst(unittest.TestCase):
    def test_freeze_support_runs_before_preflight_and_roots(self):
        entry = _load_entry("entry_portable.py")
        order = []

        def fake_freeze():
            order.append("freeze")
            raise _Stop

        def fake_roots():
            order.append("roots")

        with mock.patch.object(multiprocessing, "freeze_support", fake_freeze), \
                mock.patch.object(entry, "_install_roots", fake_roots):
            with self.assertRaises(_Stop):
                entry.main()
        self.assertEqual(order, ["freeze"])


class TestOnedirEntryFreezesFirst(unittest.TestCase):
    def test_freeze_support_runs_before_gui_main(self):
        entry = _load_entry("entry.py")

        with mock.patch.object(multiprocessing, "freeze_support",
                               side_effect=_Stop):
            with self.assertRaises(_Stop):
                entry._run()


if __name__ == "__main__":
    unittest.main(verbosity=2)
