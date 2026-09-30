# -*- coding: utf-8 -*-
"""API 路由冒烟：起一个临时服务，命中 /api/health、/api/ble/scan、/api/device/export。"""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import sys, json, threading, time
sys.path.insert(0, r"D:\EGOCourier")

import urllib.request
from ego_relay import api

def start():
    from ego_relay import db
    db.init_db()
    srv = api.ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]

srv, port = start()
base = f"http://127.0.0.1:{port}"

def get(p):
    try:
        return json.loads(urllib.request.urlopen(base + p, timeout=20).read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))

def post(p, body):
    req = urllib.request.Request(base + p, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))

print("health:", get("/api/health")["status"])
# BLE 扫描：无适配器应返回 500 + 清晰 error，而不是崩溃
print("ble/scan:", get("/api/ble/scan"))
# device/export：缺 SSH 凭证应返回 400
print("device/export:", post("/api/device/export", {"host": "", "user": "", "password": ""}))
srv.shutdown()
print("API_ROUTE_OK")
