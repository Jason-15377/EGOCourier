#!/usr/bin/env python3
"""EGO Courier 启动器：python run_server.py

也可以双击运行（需 .py 已关联 Python），或命令行执行。
启动后浏览器自动打开 http://127.0.0.1:8360
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ego_relay.api import serve

if __name__ == "__main__":
    serve(host="127.0.0.1", port=8360, open_browser=True)
