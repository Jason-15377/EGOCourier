# -*- coding: utf-8 -*-
"""Web 前端同步冒烟：抓取静态页，确认日志导出视图/导航已伺服，app.js 括号配平。"""
import sys, threading, urllib.request
sys.path.insert(0, r"D:\EGOCourier")
from ego_relay import api, db

db.init_db()
srv = api.ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
base = f"http://127.0.0.1:{port}"

def get(p):
    return urllib.request.urlopen(base + p, timeout=20).read().decode("utf-8")

html = get("/")
assert 'data-view="export"' in html, "缺日志导出导航"
assert 'id="view-export"' in html, "缺日志导出视图"
assert 'btn-ble-scan' in html and 'btn-exp' in html, "缺 BLE/导出控件"
# 导航顺序：export 在 sessions 前
assert html.index('data-view="export"') < html.index('data-view="sessions"'), "导航顺序错误"
print("index.html: 日志导出导航+视图 OK，且在会话记录之前")

js = get("/static/app.js")
for marker in ("loadExport", "scanBle", "provisionBle", "doExport",
               "/api/ble/scan", "/api/ble/provision", "/api/device/export"):
    assert marker in js, f"app.js 缺 {marker}"
# 括号/花括号配平（粗略）
for a, b in (("(", ")"), ("{", "}"), ("[", "]")):
    assert js.count(a) == js.count(b), f"app.js 括号不平衡 {a}"
print("app.js: 新接口与函数齐全，括号配平")

css = get("/static/style.css")
assert ".card" in css, "style.css 缺 .card"
print("style.css: .card OK")
srv.shutdown()
print("WEB_SYNC_OK")
