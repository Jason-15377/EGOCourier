"""Windows 原生文件/目录选择器（独立子进程运行，隔离 tkinter 主循环）。

用法: python -m ego_relay.picker <dir|file|zip>
在用户桌面上弹出系统对话框，把选中的绝对路径打印到 stdout。
"""

import sys
import tkinter as tk
from tkinter import filedialog


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "dir"
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if mode == "dir":
            path = filedialog.askdirectory(title="选择会话目录（含 SEI / MP4-pts / IMU / 日志）")
        elif mode == "zip":
            path = filedialog.askopenfilename(
                title="选择 zip 日志包", filetypes=[("zip 压缩包", "*.zip"), ("所有文件", "*.*")])
        else:
            path = filedialog.askopenfilename(
                title="选择文件", filetypes=[("日志/CSV", "*.log;*.csv;*.zip"), ("所有文件", "*.*")])
    finally:
        root.destroy()
    if path:
        print(path, flush=True)


if __name__ == "__main__":
    main()
