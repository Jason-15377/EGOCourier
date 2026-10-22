# -*- coding: utf-8 -*-
"""v1.0.7 启动链路修复的回归测试（不联网、不建真实 venv、不需要新电脑）。

对应 docs/v1.0.6_启动器修复.md 里的四个缺陷：
  D1  坏 .venv（整目录拷贝，pyvenv.cfg 的 home 指向别人机器）不能被当成已就绪；
      必须能被识别并向用户说明原因。
  D2  给虚拟环境装依赖绝不能带 --user。判断依据必须是**目标解释器**，
      而不是启动器自身所在的解释器。
  D3  装依赖必须沿用 requirements.txt 的版本约束，不能只给裸包名
      （否则新电脑会拉到 PySide6 6.12 / numpy 2.5 等越界版本）。
  D4  pip 输出要实时逐行打到窗口，不能攒到最后一次性吐出。

另外覆盖：版本越界要算作「缺失」，否则越界版本会被永远判成齐全。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import bootstrap as b
from ego_relay import deps_check as d


# ---------------------------------------------------------------------------
# D3 / 越界检测
# ---------------------------------------------------------------------------

class TestVersionCaps(unittest.TestCase):
    def test_caps_match_requirements(self):
        """VERSION_CAPS 与 requirements.txt 是两份数据，必须一致。"""
        caps = d.VERSION_CAPS
        self.assertEqual(caps["PySide6"], "6.9")
        self.assertEqual(caps["matplotlib"], "3.11")
        self.assertEqual(caps["numpy"], "2.3")

    def test_ver_tuple(self):
        self.assertEqual(d._ver_tuple("6.8.3"), (6, 8, 3))
        self.assertEqual(d._ver_tuple("2.2.6"), (2, 2, 6))
        self.assertEqual(d._ver_tuple(""), ())
        self.assertEqual(d._ver_tuple("abc"), ())

    def test_boundary_is_exclusive(self):
        """requirements 写的是 <6.9，所以 6.9 本身就算越界。"""
        self.assertTrue(d._too_new("PySide6", "6.9"))
        self.assertTrue(d._too_new("PySide6", "6.9.0"))
        self.assertFalse(d._too_new("PySide6", "6.8.3"))
        self.assertTrue(d._too_new("PySide6", "6.12.0"))
        self.assertTrue(d._too_new("numpy", "2.5.4"))
        self.assertFalse(d._too_new("numpy", "2.2.6"))
        self.assertFalse(d._too_new("numpy", None))

    def test_dev_machine_versions_are_in_range(self):
        """开发机的 6.8.3 / 3.10.9 / 2.2.6 必须不被判为越界（否则会白降级）。"""
        for pip_name, ver in (("PySide6", "6.8.3"), ("matplotlib", "3.10.9"),
                              ("numpy", "2.2.6")):
            self.assertFalse(d._too_new(pip_name, ver), f"{pip_name} {ver} 不该越界")

    def test_too_new_counts_as_missing(self):
        """装了越界版本也要算缺失，否则永远跳过安装。"""
        fake = {"PySide6": "6.12.0", "matplotlib": "3.10.9", "numpy": "2.5.4",
                "paramiko": "5.1.0", "mcap": "1.5.0", "mcap-ros2-support": "0.5.7"}
        with mock.patch.object(d, "installed_version", side_effect=fake.get), \
                mock.patch.object(d, "_importable", return_value=True):
            names = [m[0] for m in d.missing()]
        self.assertEqual(names, ["PySide6", "numpy"])

    def test_summary_flags_too_new(self):
        fake = {"PySide6": "6.12.0", "numpy": "2.2.6"}
        with mock.patch.object(d, "installed_version", side_effect=fake.get):
            rows = {name: bad for name, _ver, bad in d.summary_rows()}
        self.assertTrue(rows["PySide6"])
        self.assertFalse(rows["numpy"])


class TestPin(unittest.TestCase):
    def test_pin_uses_requirements_constraints(self):
        self.assertEqual(b.pin("PySide6"), "PySide6>=6.6,<6.9")
        self.assertEqual(b.pin("matplotlib"), "matplotlib>=3.7,<3.11")
        self.assertEqual(b.pin("numpy"), "numpy>=1.24,<2.3")
        self.assertEqual(b.pin("mcap-ros2-support"), "mcap-ros2-support>=0.5")

    def test_pin_normalizes_name(self):
        self.assertEqual(b.pin("mcap_ros2 -support".replace(" -", "-")), "mcap-ros2-support>=0.5")
        self.assertEqual(b.pin("PySide6 "), b.pin("PySide6"))

    def test_pin_passes_through_unknown(self):
        self.assertEqual(b.pin("some-unknown-pkg"), "some-unknown-pkg")

    def test_install_specs_carry_constraints(self):
        """pip_install 传给 pip 的必须是带约束的规格，而不是裸包名。"""
        seen = {}

        def fake_pip(py, args):
            seen["args"] = list(args)
            return True, ""

        with mock.patch.object(b, "_pip", fake_pip):
            b.pip_install([sys.executable], ["PySide6", "numpy"])
        self.assertIn("PySide6>=6.6,<6.9", seen["args"])
        self.assertIn("numpy>=1.24,<2.3", seen["args"])
        self.assertNotIn("PySide6", seen["args"])


# ---------------------------------------------------------------------------
# D2 / --user
# ---------------------------------------------------------------------------

class TestNoUserInVenv(unittest.TestCase):
    def test_venv_never_gets_user_flag(self):
        calls = []

        def fake_pip(py, args):
            calls.append(list(args))
            return False, "ERROR: permission denied"   # 故意制造可重试的失败

        with mock.patch.object(b, "_pip", fake_pip), \
                mock.patch.object(b, "in_venv", return_value=True):
            ok, _out = b.pip_install([sys.executable], ["numpy"])
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1, "venv 里失败不该再试 --user")
        self.assertNotIn("--user", calls[0])

    def test_system_python_retries_with_user(self):
        calls = []

        def fake_pip(py, args):
            calls.append(list(args))
            return False, "ERROR: permission denied"

        with mock.patch.object(b, "_pip", fake_pip), \
                mock.patch.object(b, "in_venv", return_value=False):
            b.pip_install([sys.executable], ["numpy"])
        self.assertEqual(len(calls), 2)
        self.assertIn("--user", calls[1])

    def test_no_user_without_permission_error(self):
        calls = []

        def fake_pip(py, args):
            calls.append(list(args))
            return False, "boom: no matching distribution"

        with mock.patch.object(b, "_pip", fake_pip), \
                mock.patch.object(b, "in_venv", return_value=False):
            b.pip_install([sys.executable], ["numpy"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--user", calls[0])

    def test_in_venv_probes_target_interpreter(self):
        """in_venv 必须探测传入的解释器，而不是当前进程。"""
        with mock.patch.object(b.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="1\n")
            self.assertTrue(b.in_venv(["X:/venv/python.exe"]))
            passed = run.call_args[0][0]
            self.assertEqual(passed[0], "X:/venv/python.exe")
        with mock.patch.object(b.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="0\n")
            self.assertFalse(b.in_venv(["python"]))

    def test_in_venv_false_on_error(self):
        with mock.patch.object(b.subprocess, "run", side_effect=OSError("nope")):
            self.assertFalse(b.in_venv(["python"]))


# ---------------------------------------------------------------------------
# D1 / 坏 venv
# ---------------------------------------------------------------------------

MISSING_HOME = os.path.join(tempfile.gettempdir(), "no_such_user_python_312")


class TestBrokenVenv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bootstrap_test_")
        self.venv = os.path.join(self.tmp, ".venv")
        os.makedirs(os.path.join(self.venv, "Scripts"))
        self.real_py = os.path.join(self.venv, "Scripts", "python.exe")
        shutil.copy2(sys.executable, self.real_py)
        self._orig = (b.VENV_DIR, b.VENV_PY)
        b.VENV_DIR, b.VENV_PY = self.venv, self.real_py

    def tearDown(self):
        b.VENV_DIR, b.VENV_PY = self._orig
        # 跑过的 python.exe 可能短暂被占用，重试几次再放弃
        for _ in range(5):
            try:
                shutil.rmtree(self.tmp)
                return
            except OSError:
                import time
                time.sleep(0.1)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_cfg(self, home):
        with open(os.path.join(self.venv, "pyvenv.cfg"), "w", encoding="utf-8") as fh:
            fh.write(f"home = {home}\n")
            fh.write("include-system-site-packages = false\nversion = 3.12.10\n")

    def test_missing_home_is_detected(self):
        self.assertFalse(os.path.isdir(MISSING_HOME), "测试前提：该路径必须不存在")
        self._write_cfg(MISSING_HOME)
        # 文件在 → 旧逻辑会误判；新逻辑必须判为不可用
        self.assertTrue(os.path.isfile(self.real_py))
        self.assertFalse(b.venv_ready())
        reason = b.broken_venv_reason()
        self.assertIn("不存在", reason)
        self.assertIn(MISSING_HOME, reason)

    def test_missing_interpreter_is_detected(self):
        self._write_cfg(os.path.dirname(sys.executable))
        # 不真拷 exe：Windows 上被跑过的 python.exe 会被占用而删不掉
        os.remove(self.real_py)
        with open(self.real_py, "w"):
            pass
        os.remove(self.real_py)
        self.assertFalse(b.venv_ready())
        self.assertIn("python.exe", b.broken_venv_reason())

    def test_valid_home_is_ready(self):
        self._write_cfg(os.path.dirname(sys.executable))
        self.assertTrue(b.venv_ready())
        self.assertEqual(b.broken_venv_reason(), "")

    def test_valid_home_without_runnable_exe(self):
        """home 存在但 exe 跑不起来（损坏）时也要判为不可用。"""
        self._write_cfg(os.path.dirname(sys.executable))
        with open(self.real_py, "wb") as fh:
            fh.write(b"not a real exe")
        self.assertFalse(b.venv_ready())
        self.assertIn("无法执行", b.broken_venv_reason())

    def test_remove_broken_venv(self):
        self._write_cfg(MISSING_HOME)
        self.assertEqual(b.remove_broken_venv(), "")
        self.assertFalse(os.path.isdir(self.venv))

    def test_remove_failure_is_reported(self):
        with mock.patch.object(b.shutil, "rmtree", side_effect=OSError("in use")):
            self.assertIn("in use", b.remove_broken_venv())


# ---------------------------------------------------------------------------
# D4 / 实时输出
# ---------------------------------------------------------------------------

class _FakePipe:
    """模拟 pip 的 stdout：_pip 用 readline 逐行读，直到空串。"""

    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else ""

    def close(self):
        pass


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = _FakePipe(lines)
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


class TestPipStreamsOutput(unittest.TestCase):
    def test_lines_are_forwarded_and_collected(self):
        with mock.patch.object(b.subprocess, "Popen",
                               return_value=_FakeProc(["alpha\n", "beta\n"])), \
                mock.patch("builtins.print") as pr:
            ok, out = b._pip([sys.executable], ["install", "numpy"])
        self.assertTrue(ok)
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        printed = " ".join(str(c.args[0]) for c in pr.call_args_list if c.args)
        self.assertIn("alpha", printed, "输出必须实时打到窗口，而不是只收集")

    def test_install_gets_progress_bar_off(self):
        with mock.patch.object(b.subprocess, "Popen",
                               return_value=_FakeProc([])) as popen, \
                mock.patch("builtins.print"):
            b._pip([sys.executable], ["install", "numpy"])
        cmd = popen.call_args[0][0]
        self.assertIn("--progress-bar", cmd)
        self.assertEqual(cmd[cmd.index("--progress-bar") + 1], "off")

    def test_failure_keeps_full_output(self):
        with mock.patch.object(b.subprocess, "Popen",
                               return_value=_FakeProc(["ERROR: no matching\n"],
                                                      returncode=1)), \
                mock.patch("builtins.print"):
            ok, out = b._pip([sys.executable], ["install", "numpy"])
        self.assertFalse(ok)
        self.assertIn("ERROR: no matching", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
