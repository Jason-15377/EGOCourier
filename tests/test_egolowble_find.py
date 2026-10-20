"""EgoLowBle.dll 定位：源码模式与打包（frozen）模式都必须找得到随包携带的那份。

历史问题：打包后 `__file__` 位于 PyInstaller 解包目录（sys._MEIPASS），
而 DLL 由 spec 放进 `<MEIPASS>/lib/`。旧实现只查「项目根/lib/」即
「<MEIPASS>/../lib/」，于是 exe 在新电脑上永远报「未找到 EgoLowBle.dll」。
"""
import contextlib
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ego_relay import egolowble  # noqa: E402

_MISSING = object()


@contextlib.contextmanager
def _fake_sys(meipass: str, exe: str):
    """模拟 PyInstaller 运行环境（sys.frozen / _MEIPASS / executable）。"""
    keys = ("frozen", "_MEIPASS", "executable")
    old = {k: getattr(sys, k, _MISSING) for k in keys}
    sys.frozen = True
    sys._MEIPASS = meipass
    sys.executable = exe
    try:
        yield
    finally:
        for k, v in old.items():
            if v is _MISSING:
                try:
                    delattr(sys, k)
                except AttributeError:
                    pass
            else:
                setattr(sys, k, v)


@contextlib.contextmanager
def _isolated_env(module_file=None):
    """关掉环境变量 / 配置 / 盘符探测的干扰，只验证路径候选。

    源码模式下 find_dll 会用本模块的 __file__ 推出项目根去查 lib/，
    而本仓库确实带 DLL，会污染「找不到」的断言，所以可一并替换 __file__。
    """
    patches = [
        mock.patch.object(egolowble, "_find_in_dir", return_value=""),
        mock.patch.object(egolowble, "_candidate_drives", return_value=[]),
        mock.patch.object(egolowble.config, "EGOLOWBLE_DLL", ""),
        mock.patch.dict(os.environ, {"EGOLOWBLE_DLL": ""}),
    ]
    if module_file:
        patches.append(mock.patch.object(egolowble, "__file__", module_file))
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()


class TestFindDllFrozen(unittest.TestCase):
    def _fake_app(self, with_dll=True):
        """造一个便携版目录：<root>/EGO Courier.exe + <root>/runtime/lib/EgoLowBle.dll"""
        root = tempfile.mkdtemp(prefix="pkg_")
        meipass = os.path.join(root, "runtime")
        libdir = os.path.join(meipass, "lib")
        os.makedirs(libdir)
        dll = os.path.join(libdir, "EgoLowBle.dll")
        if with_dll:
            with open(dll, "wb") as fh:
                fh.write(b"MZ")
        exe = os.path.join(root, "EGO Courier.exe")
        open(exe, "wb").close()
        return root, meipass, dll, exe

    def test_uses_meipass_lib(self):
        _root, meipass, dll, exe = self._fake_app()
        with _fake_sys(meipass, exe), _isolated_env():
            self.assertEqual(os.path.normcase(egolowble.find_dll()),
                             os.path.normcase(dll))

    def test_missing_dll_returns_blank(self):
        _root, meipass, _dll, exe = self._fake_app(with_dll=False)
        fake_mod = os.path.join(meipass, "ego_relay", "egolowble.py")
        with _fake_sys(meipass, exe), _isolated_env(fake_mod):
            self.assertEqual(egolowble.find_dll(), "")

    def test_exe_side_lib_is_accepted(self):
        """用户自行在 exe 同级放 lib/EgoLowBle.dll 时也应能找到。"""
        root, meipass, _dll, exe = self._fake_app(with_dll=False)
        side = os.path.join(root, "lib")
        os.makedirs(side)
        side_dll = os.path.join(side, "EgoLowBle.dll")
        with open(side_dll, "wb") as fh:
            fh.write(b"MZ")
        fake_mod = os.path.join(meipass, "ego_relay", "egolowble.py")
        with _fake_sys(meipass, exe), _isolated_env(fake_mod):
            self.assertEqual(os.path.normcase(egolowble.find_dll()),
                             os.path.normcase(side_dll))

    def test_explicit_path_wins(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "custom.dll")
            open(p, "wb").close()
            self.assertEqual(os.path.normcase(egolowble.find_dll(p)),
                             os.path.normcase(p))


class TestNoHardcodedPaths(unittest.TestCase):
    def test_source_has_no_machine_specific_absolute_paths(self):
        """不能写死某台机器上的绝对路径（盘符 + 具体目录），那在别人电脑上不存在。"""
        with open(egolowble.__file__, encoding="utf-8") as fh:
            src = fh.read()
        pattern = r'["\']([A-Za-z]:[\\/][^"\']*)["\']'
        bad = [m.group(0) for m in re.finditer(pattern, src)]
        self.assertEqual(bad, [], f"不应写死绝对路径：{bad}")


if __name__ == "__main__":
    unittest.main()
