#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EGO Courier 便携版启动入口（launcher + runtime 形态）。

与 ``packaging/entry.py``（onedir 版）的区别：本入口是**给测试同事用的**，
目标机器可能既没装 Python，也没装 EGOViewer / ffmpeg，因此除了把应用拉起来，
还要在出问题时给出人能看懂的提示，而不是黑框一闪就退。

导入顺序（保证「改 .py 立即生效」）：

1. ``sys._MEIPASS/app`` —— 明文源码（优先，现场可直接改这几个文件）；
2. ``sys._MEIPASS``     —— PyInstaller 解压目录（兜底，源码被裁掉时用）；
3. 源码模式的项目根      —— 便于开发期直接跑本文件。
"""

from __future__ import annotations

import os
import sys


def _roots() -> list[str]:
    """按优先级返回可导入 ego_relay / ego_gui 的目录。"""
    out: list[str] = []

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            out.append(os.path.join(meipass, "app"))   # 明文源码优先
            out.append(meipass)                        # 兜底
    else:
        out.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    return [p for p in out if os.path.isdir(p)]


def _install_roots() -> None:
    for p in reversed(_roots()):
        if p not in sys.path:
            sys.path.insert(0, p)


def _fatal(title: str, message: str, detail: str = "") -> int:
    """启动失败时弹窗提示（无 GUI 环境则退化为 stderr）。"""
    text = message
    if detail:
        text += "\n\n详细信息：\n" + detail
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, text, f"EGO Courier — {title}", 0x10  # MB_ICONERROR
        )
    except Exception:
        print(f"[{title}] {text}", file=sys.stderr)
    return 1


def _preflight() -> str:
    """启动自检：返回错误信息（空串 = 通过）。

    只拦「必装且缺失会直接崩」的项目；EGOViewer / ffmpeg 这类「功能相关但
    非必需」的依赖，交给程序内的页签自行提示，不阻塞启动。
    """
    try:
        import PySide6  # noqa: F401
    except Exception as exc:
        return f"缺少图形界面依赖 PySide6（{exc}）。请确认 runtime/ 目录完整，未被杀软删除。"

    try:
        from ego_relay import config  # noqa: F401
        from ego_relay import db
    except Exception as exc:
        return f"核心模块 ego_relay 导入失败（{exc}）。请确认 runtime/app 目录存在。"

    try:
        db.init_db()
    except Exception as exc:
        return (
            f"无法初始化数据库（{exc}）。\n"
            f"数据目录：{getattr(config, 'DATA_ROOT', '?')}\n"
            "该目录可能被占用或没有写入权限。"
        )

    return ""


def main() -> int:
    _install_roots()

    problem = _preflight()
    if problem:
        return _fatal("启动失败", problem)

    try:
        from ego_gui.main import main as gui_main
    except Exception as exc:
        import traceback

        return _fatal("启动失败", "加载界面失败。", traceback.format_exc())

    ms = _selftest_ms()
    if not ms:
        try:
            return gui_main()
        except Exception:
            import traceback

            return _fatal("运行中断", "程序运行时发生未处理的异常。", traceback.format_exc())

    # 自检：singleShot(0) 在事件循环启动后立刻触发，此时再挂 3 秒退出计时，
    # 避免 QTimer 在 loop 启动前就到期（那种写法会一直卡在事件循环里）。
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    import ego_gui.main as _m

    _orig_exec = QApplication.exec

    def _exec_with_timeout():
        app = QApplication.instance()
        QTimer.singleShot(0, lambda: QTimer.singleShot(ms, app.quit))
        return _orig_exec()

    try:
        # QApplication.exec 在 PySide6 里是 staticmethod，替换时须保持同样的描述符
        QApplication.exec = staticmethod(_exec_with_timeout)
    except (TypeError, AttributeError):
        return _fatal("自检失败", "无法挂载自检退出计时（该环境不支持替换 exec）。")

    try:
        return _m.main()
    except Exception:
        import traceback

        return _fatal("运行中断", "程序运行时发生未处理的异常。", traceback.format_exc())


def _selftest_ms() -> int:
    """``EGO_SELFTEST_EXIT_MS``：打包自检用，界面起来后自动退出。

    没有它就只能在发布脚本里人眼看窗口，CI/脚本无法判断「能启动」。
    非自检场景该变量不存在，行为与普通启动完全一致。
    """
    try:
        return max(0, int(os.environ.get("EGO_SELFTEST_EXIT_MS", "") or 0))
    except ValueError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
