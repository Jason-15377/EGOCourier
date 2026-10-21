#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EGO Courier 启动器：自动找 Python → 准备运行环境 → 装依赖 → 启动 GUI。

双击 ``EGO Courier.bat`` 时实际跑的就是本文件。设计目标是让一台**全新电脑**
也能一次点开：原先 bat 只是 ``python run_desktop.py``，而仓库里没有依赖清单，
新机器上必然满屏 ModuleNotFoundError，随后黑框一闪就关。

工作流程
--------
1. 检查 Python 版本（3.10–3.12；3.13+ 直接说明「请换 3.12」，而不是让 pip 刷一屏红字）；
2. 运行环境：优先用本目录的 ``.venv``（不污染系统 Python）；没有就自动创建，
   创建失败则退回系统 Python + ``--user`` 安装；
3. 依赖检查：只装缺失的包，全部就绪则直接启动（日常启动无感知）；
4. 装不上：给中文原因与两条退路（内网镜像 / 离线 wheel），并等回车，不闪退；
5. 启动：界面正常则本窗口保持打开供看日志，退出时回车关闭；崩溃则保留报错。

命令行
------
    python bootstrap.py                 启动 GUI（缺依赖时自动安装）
    python bootstrap.py --check         只做体检并打印报告，不安装不启动
    python bootstrap.py --no-install    缺依赖就报错退出，不自动安装
    python bootstrap.py --reinstall     强制重新安装全部依赖
    python bootstrap.py --system-python 不用 .venv，直接用当前 Python
"""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REQ_FILE = os.path.join(HERE, "requirements.txt")
VENV_DIR = os.path.join(HERE, ".venv")
VENV_PY = os.path.join(VENV_DIR, "Scripts", "python.exe")
LOG_DIR = os.path.join(HERE, "log")
RUN_LOG = os.path.join(LOG_DIR, "launcher.log")
PIP_TIMEOUT_S = 60 * 45          # 全新安装 PySide6+matplotlib 约 200MB，慢网要留足
PIP_MIRROR = os.environ.get("EGO_PIP_INDEX_URL", "").strip()  # 内网镜像可选
APP_VERSION = "1.0.6"

if HERE not in sys.path:
    sys.path.insert(0, HERE)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def ensure_stdout() -> None:
    """双击 bat 时控制台是 GBK；强制 UTF-8，避免中文提示变乱码或吞掉步骤。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def hr(title: str = "") -> None:
    print("-" * 68)
    if title:
        print(title)


def pause(msg: str = "按回车键关闭此窗口…") -> None:
    try:
        input("\n" + msg)
    except (EOFError, KeyboardInterrupt):
        pass


def write_run_log(lines: list[str]) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(RUN_LOG, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Python 解释器
# ---------------------------------------------------------------------------

def _probe(cmd: list[str]) -> str:
    """返回 "版本|exe"，失败返回空串。"""
    try:
        r = subprocess.run(
            cmd + ["-c", "import sys;print(sys.version.split()[0]+'|'+sys.executable)"],
            capture_output=True, text=True, timeout=20)
    except Exception:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _py_launcher() -> list[str] | None:
    """``py -3``：Windows 上能绕过 PATH 里的 Microsoft Store 占位程序。"""
    if os.name != "nt":
        return None
    return ["py", "-3"] if _probe(["py", "-3"]) else None


def find_system_python() -> list[str] | None:
    cands = [[sys.executable], ["python3"]] if os.name != "nt" else [["python"], _py_launcher()]
    for cmd in cands:
        if cmd and _probe(cmd):
            return cmd
    return None


def describe(cmd: list[str]) -> str:
    info = _probe(cmd)
    if not info:
        return "未知版本"
    ver, exe = info.split("|", 1)
    return f"{ver}  ({exe})"


def in_this_venv() -> bool:
    return os.path.normcase(os.path.dirname(sys.executable)) == \
        os.path.normcase(os.path.dirname(VENV_PY))


# ---------------------------------------------------------------------------
# venv
# ---------------------------------------------------------------------------

def venv_ready() -> bool:
    return os.path.isfile(VENV_PY)


def create_venv(py: list[str]) -> tuple[bool, str]:
    hr("首次运行：正在创建独立运行环境 .venv（只需一次，约 10–60 秒）")
    print("PySide6 / matplotlib 等依赖约 200MB，装进 .venv 不会影响系统里其它 Python 项目。\n")
    try:
        r = subprocess.run(py + ["-m", "venv", VENV_DIR],
                           capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
    except Exception as exc:
        return False, str(exc)
    if r.returncode == 0 and venv_ready():
        print("[OK] .venv 创建完成。\n")
        return True, ""
    detail = ((r.stderr or "") + (r.stdout or "")).strip()
    return False, detail[-800:] or "未知错误"


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------

def _pip(py: list[str], args: list[str]) -> tuple[bool, str]:
    cmd = py + ["-m", "pip", "--disable-pip-version-check", "--no-input"] + args
    if PIP_MIRROR and "install" in args:
        cmd += ["-i", PIP_MIRROR]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT_S,
                           encoding="utf-8", errors="replace")
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return False, f"安装超时（超过 {PIP_TIMEOUT_S // 60} 分钟），可能是网络过慢。"
    except Exception as exc:
        return False, str(exc)


def pip_install(py: list[str], packages: list[str], user: bool) -> tuple[bool, str]:
    return _pip(py, ["install", "--upgrade"] + packages + (["--user"] if user else []))


def check(py: list[str]) -> list[tuple[str, str, str]]:
    """在目标解释器里跑依赖探测，返回缺失项 [(显示名, pip 名, 影响)]。"""
    code = (
        "import sys;sys.path.insert(0, %r)\n"
        "from ego_relay import deps_check as d\n"
        "for n, pip, req, why in d.missing():\n"
        "    print('MISSING|%%s|%%s|%%s' %% (n, pip, why))\n"
    ) % HERE
    try:
        r = subprocess.run(py + ["-c", code], capture_output=True, text=True,
                           timeout=120, encoding="utf-8", errors="replace")
    except Exception as exc:
        return [("依赖探测", "-", f"探测脚本无法运行：{exc}")]
    if r.returncode != 0:
        tail = ((r.stderr or "").strip().splitlines() or ["未知错误"])[-1]
        return [("依赖探测", "-", f"探测脚本失败：{tail}")]
    out = []
    for line in r.stdout.splitlines():
        if line.startswith("MISSING|"):
            parts = line.split("|", 3)
            out.append((parts[1], parts[2], parts[3] if len(parts) > 3 else ""))
    return out


def table(py: list[str]) -> None:
    code = (
        "import sys;sys.path.insert(0, %r)\n"
        "from ego_relay import deps_check as d\n"
        "print('PYTHON|' + sys.version.split()[0])\n"
        "for n, v, bad in d.summary_rows():\n"
        "    print('ROW|%%s|%%s|%%d' %% (n, v, bad))\n"
    ) % HERE
    try:
        r = subprocess.run(py + ["-c", code], capture_output=True, text=True,
                           timeout=120, encoding="utf-8", errors="replace")
        lines = r.stdout.splitlines()
    except Exception:
        lines = []
    print("依赖检查：")
    for line in lines:
        if line.startswith("ROW|"):
            _, name, ver, bad = line.split("|", 3)
            print(f"   {'[缺失]' if bad == '1' else '[ OK ]'}  {name:<20} {ver}")
    print()


def explain_pip_failure(output: str) -> str:
    """把 pip 的中英文报错收敛成一句可行动的建议。"""
    low = output.lower()
    if "no matching distribution" in low or "could not find a version" in low:
        if "pyside6" in low or "matplotlib" in low:
            return ("PySide6 / matplotlib 没有适用于当前 Python 版本的预编译包。\n"
                    "最常见原因是 Python 版本过新（3.13 / 3.14），请改装 Python 3.12 后重试。")
        return "pip 源里找不到所需版本：请确认网络可达 PyPI，或配置内网镜像。"
    if any(k in low for k in ("connection", "timed out", "ssl", "proxy", "unreachable",
                              "temporary failure", "read timed out")):
        return ("连接 PyPI 失败（公司网络通常需要代理或内网镜像）。\n"
                "可先设置环境变量 EGO_PIP_INDEX_URL 指向内网 pip 源，再重新双击启动；\n"
                "或把 requirements.txt 中各个包的 .whl 下载到本目录后离线安装。")
    if "permission" in low or "access is denied" in low or "winerror 5" in low:
        return "没有写入权限：请把程序解压到自己的目录（如 D:\\EGO Courier），不要放在 C:\\Program Files。"
    if "no space" in low or "errno 28" in low:
        return "磁盘空间不足：依赖约需 1GB 空闲空间。"
    return "pip 安装失败，完整输出已保存到 log\\install.log。"


def save_install_log(output: str) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "install.log")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(output)
    except Exception:
        pass
    return path


def fatal(title: str, *lines: str) -> int:
    hr(f"[!] {title}")
    body = "\n".join(lines)
    print(body)
    write_run_log([f"=== {title} ===", body])
    pause()
    return 1


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def launch_gui(py: list[str]) -> int:
    runner = [VENV_PY] if in_this_venv() else py
    hr("启动 EGO Courier …（本窗口会在退出程序后自动关闭；保留它可查看日志）")
    sys.stdout.flush()
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    try:
        r = subprocess.run(runner + [os.path.join(HERE, "run_desktop.py")],
                           cwd=HERE, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    except Exception as exc:
        return fatal("启动失败", f"无法运行界面：{exc}")

    # 调试输出不刷屏，只落盘；只有失败才展示
    if r.stdout or r.stderr:
        write_run_log(["=== GUI run ===", r.stdout or "", r.stderr or ""])

    if r.returncode == 0:
        return 0
    stderr_tail = "\n".join((r.stderr or "").strip().splitlines()[-25:])
    return fatal(
        f"界面异常退出（退出码 {r.returncode}）",
        "请把以下两处信息截图发给开发：",
        f"  · {RUN_LOG}",
        "  · 下面的报错文本",
        "",
        stderr_tail or "（界面没有输出报错文本，请查看 log\\gui_launch.log）",
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ensure_stdout()
    check_only = "--check" in argv
    no_install = "--no-install" in argv
    reinstall = "--reinstall" in argv
    force_system = "--system-python" in argv

    from ego_relay import deps_check

    if not check_only:
        hr()
        print(f"   EGO Courier  v{APP_VERSION}   启动器")
        hr()

    # ---- Python 版本（当前解释器已不合适就不必继续）
    problem = deps_check.python_problem()
    if problem:
        msg = [problem, "",
               "（若刚才用 EGO Courier.bat 启动，它会优先寻找 3.10–3.12 的 Python；"
               "这里报错说明本机只装了不兼容的版本。）"]
        if check_only:
            print("\n".join(msg))
            return 2
        return fatal("Python 版本不合适", *msg)

    # ---- 选择解释器
    py: list[str]
    in_venv = False
    if in_this_venv() or force_system:
        py = [sys.executable]
        env_label = "系统 Python" if force_system else ".venv（本目录独立环境）"
    elif venv_ready():
        py = [VENV_PY]
        in_venv = True
        env_label = ".venv（本目录独立环境）"
    else:
        sys_py = find_system_python()
        if sys_py is None:
            msg = [
                "本机没有找到 Python。桌面版需要 Python 3.12 才能运行。",
                "",
                "1. 打开 https://www.python.org/downloads/release/python-31210/",
                "   下载 “Windows installer (64-bit)”。",
                "2. 安装时务必勾选 “Add python.exe to PATH”，其余保持默认。",
                "3. 安装完成后重新双击 EGO Courier.bat。",
                "",
                "（如果已拿到便携版 zip，请用其中的 EGO Courier.exe，无需 Python。）",
            ]
            if check_only:
                print("\n".join(msg))
                return 2
            return fatal("未找到 Python", *msg)
        problem = deps_check.python_problem(_probe_version(sys_py))
        if problem and not force_system:
            return fatal("Python 版本不合适", problem)
        ok, err = create_venv(sys_py)
        if ok:
            py, in_venv, env_label = [VENV_PY], True, ".venv（本目录独立环境）"
        else:
            print("[提示] 无法创建 .venv，改为直接用系统 Python 安装依赖。")
            if err:
                print("       原因：" + err.splitlines()[-1][:200])
            py, env_label = sys_py, "系统 Python（--user 安装）"

    print("Python：" + describe(py))
    print("环境  ：" + env_label + "\n")

    if not os.path.isfile(REQ_FILE):
        return fatal("安装包不完整", "requirements.txt 不在程序目录，请重新解压完整的 zip。")

    # ---- 依赖
    if check_only:
        table(py)
        missing = check(py)
        for name, _pip, why in missing:
            print(f"   缺少 {name}：{why}")
        print("\n结论：" + ("依赖齐全，可直接启动。" if not missing else "存在缺失，双击启动时会自动安装。"))
        return 0 if not missing else 2

    missing = check(py)
    if reinstall and not missing:
        missing = [("全部依赖", "", "按 --reinstall 强制重装")]

    if missing:
        if no_install:
            return fatal("缺少运行依赖", *[f"   缺少 {n}：{w}" for n, _p, w in missing],
                         "", f'手动安装：python -m pip install -r "{REQ_FILE}"')

        pkgs = list(dict.fromkeys(p for _n, p, _w in missing if p))
        hr(f"检测到缺少 {len(pkgs)} 个依赖，正在自动安装（仅首次需要，请保持联网）：")
        print("   " + "  ".join(pkgs))
        print("   约 200MB，视网速 1–10 分钟；窗口会静止一段时间，属正常现象。\n")
        ok, output = pip_install(py, pkgs if pkgs else ["-r", REQ_FILE], user=not in_this_venv())
        if not ok:
            log = save_install_log(output)
            return fatal("依赖安装失败", explain_pip_failure(output),
                         f"\n完整日志：{log}",
                         "\n也可以手动安装：",
                         f'   python -m pip install -r "{REQ_FILE}"')

        still = check(py)
        if still:
            return fatal("依赖仍未就绪",
                         *[f"   缺少 {n}：{w}" for n, _p, w in still],
                         "",
                         "可能原因：pip 装到了另一个 Python，或安全软件删掉了文件。",
                         f"详细日志：{save_install_log(output)}")
        print("[OK] 依赖安装完成。\n")
    else:
        print("[OK] 依赖齐全，直接启动。\n")

    return launch_gui(py)


def _probe_version(py: list[str]) -> tuple[int, int, int]:
    info = _probe(py)
    ver = info.split("|", 1)[0] if info else ""
    from ego_relay import deps_check
    return deps_check.version_tuple(ver) if ver else (0, 0, 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
