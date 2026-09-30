"""HTTP API（纯标准库 http.server 实现 REST 服务 + 静态前端）。

主入口：python -m ego_relay.api [--port N]  或  CLI `ego server`
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import config, db
from . import analysis, export, parsers
from . import __version__

WEB_DIR = Path(__file__).resolve().parent / "web"

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _json_ok(body: dict, status: int = 200) -> tuple:
    return (status, json.dumps(body, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8")


def _json_err(msg: str, status: int = 400) -> tuple:
    return (status, json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes, ctype: str, extra: dict = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> Optional[dict]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def log_message(self, *a):  # 静默访问日志，避免刷屏
        pass

    # ---------------------------------------------------------------- routing
    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_PUT(self):
        self._route()

    def _route(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        q = parse_qs(parsed.query)
        try:
            self._dispatch(self.command, path, q)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass

    def _dispatch(self, method, path, q):
        # 静态前端
        if method == "GET" and (path == "/" or path.startswith("/static/") or not path.startswith("/api")):
            self._serve_static(path)
            return

        # API
        if path == "/api/health":
            return self._json(200, {"status": "ok", "version": __version__,
                                    "db": str(config.DB_PATH)})
        if method == "GET" and path.startswith("/api/pick"):
            return self._api_pick(q)
        if method == "POST" and path == "/api/import":
            return self._api_import()
        if path == "/api/sessions":
            return self._api_sessions()
        if path == "/api/devices":
            return self._json(200, db.query("SELECT * FROM devices ORDER BY id"))
        if method == "GET" and path == "/api/wifi/scan":
            from . import wifi
            try:
                return self._json(200, wifi.scan_with_status())
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": f"WiFi 扫描失败: {e}"})
        if path == "/api/ble/scan":
            if method == "GET":
                return self._api_ble_scan()
            return self._json(405, {"error": "method not allowed"})
        if path == "/api/ble/provision":
            if method == "POST":
                return self._api_ble_provision()
            return self._json(405, {"error": "method not allowed"})
        if path == "/api/device/export":
            if method == "POST":
                return self._api_device_export()
            return self._json(405, {"error": "method not allowed"})
        if path == "/api/device/probe":
            if method == "GET":
                return self._api_device_probe(q)
            return self._json(405, {"error": "method not allowed"})
        if method == "GET" and path.startswith("/api/anomalies/"):
            return self._api_anomaly_image(path[len("/api/anomalies/"):])
        if path.startswith("/api/traces/"):
            return self._api_trace(path[len("/api/traces/"):])
        if path == "/api/sessions/compare":
            if method == "POST":
                return self._api_sessions_compare()
            return self._json(405, {"error": "method not allowed"})
        if path.startswith("/api/sessions/"):
            return self._api_session(method, path, q)
        if path == "/api/config/thresholds":
            if method == "GET":
                return self._json(200, {"thresholds_us": _load_thresholds()})
            if method == "PUT":
                return self._api_thresholds()
        self._json(404, {"error": "not found"})

    # ---------------------------------------------------------------- handlers
    def _api_anomaly_image(self, rest):
        """返回异常截帧 jpg。rest 形如 '<id>/image'。"""
        parts = rest.split("/")
        if len(parts) < 2 or parts[1] != "image":
            return self._json(404, {"error": "not found"})
        try:
            aid = int(parts[0])
        except ValueError:
            return self._json(400, {"error": "非法 anomaly id"})
        a = db.query_one("SELECT frame_image FROM anomalies WHERE id=?", (aid,))
        if not a or not a.get("frame_image"):
            return self._json(404, {"error": "无截帧"})
        fp = Path(a["frame_image"])
        if not fp.is_file():
            return self._json(404, {"error": "截帧文件不存在"})
        return self._send(200, fp.read_bytes(), "image/jpeg")

    def _api_pick(self, q):
        """弹出 Windows 原生目录/文件选择框，返回绝对路径。"""
        mode = q.get("mode", ["dir"])[0]
        if mode not in ("dir", "file", "zip"):
            mode = "dir"
        try:
            import subprocess
            import sys
            r = subprocess.run([sys.executable, "-m", "ego_relay.picker", mode],
                               capture_output=True, text=True, timeout=180)
            path = r.stdout.strip()
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": f"选择器失败: {e}"})
        if not path:
            return self._json(200, {"path": None})  # 用户取消
        return self._json(200, {"path": path})

    def _api_import(self):
        body = self._read_json()
        if not body or not body.get("path"):
            return self._json(400, {"error": "缺少 path（本地目录或 zip 路径）"})
        try:
            ps = parsers.import_path(body["path"])
        except FileNotFoundError as e:
            return self._json(404, {"error": str(e)})
        thr = body.get("thresholds_us") or None
        r = analysis.align_and_analyze(ps, thr)
        s = db.get_session(r["session_id"])
        return self._json(200, {"session": s, "stats": db.session_stats(r["session_id"])})

    def _api_sessions(self):
        return self._json(200, {"sessions": db.list_sessions(500)})

    def _api_sessions_compare(self):
        from . import compare
        body = self._read_json()
        ids = body.get("ids") or [] if body else []
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            return self._json(400, {"error": "ids 需为整数数组"})
        if len(ids) < 1:
            return self._json(400, {"error": "ids 至少 1 个"})
        rows = compare.compare_sessions(ids)
        return self._json(200, {"rows": compare.compare_table_rows(rows), "count": len(rows)})

    def _api_trace(self, trace_id):
        trace_id = trace_id.strip()
        if not trace_id:
            return self._json(400, {"error": "trace_id 为空"})
        sess = db.query(
            "SELECT DISTINCT session_id FROM frame_entries WHERE trace_id=? "
            "UNION SELECT session_id FROM trace_sessions WHERE trace_id=?",
            (trace_id, trace_id))
        ids = [r["session_id"] for r in sess]
        frames = []
        if ids:
            frames = db.query(
                "SELECT * FROM frame_entries WHERE trace_id=? ORDER BY frame_index LIMIT 2000",
                (trace_id,))
        return self._json(200, {"trace_id": trace_id, "sessions": ids, "frames": frames})

    def _api_session(self, method, path, q):
        rest = path[len("/api/sessions/"):]
        parts = rest.split("/")
        try:
            sid = int(parts[0])
        except (ValueError, IndexError):
            return self._json(400, {"error": "非法 session id"})
        action = parts[1] if len(parts) > 1 else ""
        s = db.get_session(sid)
        if not s:
            return self._json(404, {"error": "会话不存在"})

        if method == "GET" and not action:
            return self._json(200, {"session": s, "stats": db.session_stats(sid)})
        if method == "GET" and action == "frames":
            side = q.get("side", [None])[0]
            frame_index = q.get("frame_index", [None])[0]
            anomaly_only = q.get("anomaly_only", ["0"])[0] == "1"
            limit = int(q.get("limit", ["2000"])[0])
            frames = db.session_frames(sid, side=side,
                                       frame_index=int(frame_index) if frame_index else None)
            if anomaly_only:
                anom_keys = {(a["frame_index"], a["side"]) for a in db.session_anomalies(sid)
                             if a["side"] != "frame"}
                # 也包含以帧为单位的异常（app_receive_delay side=frame）所在帧
                frames = [f for f in frames
                          if (f["frame_index"], f["side"]) in anom_keys
                          or f["frame_index"] in {a["frame_index"] for a in db.session_anomalies(sid)
                                                  if a["side"] == "frame"}]
            return self._json(200, {"frames": frames[:limit], "total": len(frames)})
        if method == "GET" and action == "anomalies":
            rule = q.get("rule", [None])[0]
            severity = q.get("severity", [None])[0]
            return self._json(200, {"anomalies": db.session_anomalies(sid, rule=rule,
                                                                      severity=severity)})
        if method == "GET" and action == "export":
            trace_id = q.get("trace", [None])[0]
            zpath = export.export_session(sid, trace_id=trace_id)
            data = Path(zpath).read_bytes()
            return self._send(200, data,
                              "application/zip",
                              {"Content-Disposition": f'attachment; filename="{Path(zpath).name}"'})
        if method == "POST" and action == "reparse":
            thr = self._read_json() or {}
            r = analysis.recompute_session(sid, thr.get("thresholds_us") or None)
            return self._json(200, {"session_id": sid, **r})
        if method == "POST" and action == "attach":
            body = self._read_json()
            if not body or not body.get("path"):
                return self._json(400, {"error": "缺少 path（ego-viewer-*.log 路径）"})
            try:
                r = analysis.attach_app_log(sid, body["path"])
            except FileNotFoundError as e:
                return self._json(404, {"error": str(e)})
            return self._json(200, {"session_id": sid, **r})
        self._json(404, {"error": "unknown action"})

    def _api_thresholds(self):
        body = self._read_json()
        if not body or "thresholds_us" not in body:
            return self._json(400, {"error": "缺少 thresholds_us"})
        _save_thresholds(body["thresholds_us"])
        return self._json(200, {"thresholds_us": _load_thresholds()})

    # ---------------------------------------------------------------- BLE 配网
    def _api_ble_scan(self):
        from . import ble_provision
        try:
            return self._json(200, {"available": True, "devices": ble_provision.scan_devices()})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": f"BLE 扫描失败: {e}"})

    def _api_ble_provision(self):
        from . import ble_provision
        body = self._read_json()
        if not body or not body.get("address") or not body.get("ssid"):
            return self._json(400, {"error": "缺少 address（蓝牙地址）或 ssid"})
        try:
            r = ble_provision.provision(body["address"], body["ssid"],
                                        body.get("password", ""))
            return self._json(200, r)
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": f"配网失败: {e}"})

    def _api_device_probe(self, q):
        """导出前连通性预检：同网段 + ping 设备 IP。"""
        host = (q.get("host") or [None])[0]
        if not host:
            return self._json(400, {"error": "缺少 host（设备 IP）"})
        try:
            from . import wifi
            return self._json(200, wifi.probe_device(host))
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": f"连通性预检失败: {e}"})

    def _api_device_export(self):
        from . import sftp_pull
        body = self._read_json() or {}
        host = body.get("host") or config.DEVICE_IP
        user = body.get("user") or config.SSH_USER
        pwd = body.get("password") or config.SSH_PASSWORD
        if not host or not user or not pwd:
            return self._json(400, {"error": "缺少 host/user/password（SSH 凭证）"})
        remote_dirs = body.get("remote_dirs") or ["/ego/logs"]
        local = body.get("local_dir") or str(config.EXPORT_DIR)
        zip_first = body.get("zip_first", True)
        auto_import = body.get("auto_import", True)
        try:
            files = sftp_pull.pull_logs(host, user, pwd, remote_dirs, local,
                                        zip_first=zip_first,
                                        port=int(body.get("port") or config.SSH_PORT))
            result = {"files": files}
            if auto_import and files:
                result["import"] = sftp_pull.import_pulled(files[0])
            return self._json(200, result)
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": f"导出失败: {e}"})

    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        elif path.startswith("/static/"):
            path = "/" + path[len("/static/"):]
        rel = path.lstrip("/")
        # 防止目录穿越
        fp = (WEB_DIR / rel).resolve()
        if not str(fp).startswith(str(WEB_DIR.resolve())) or not fp.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = _MIME.get(fp.suffix.lower(), "application/octet-stream")
        self._send(200, fp.read_bytes(), ctype)


# 阈值持久化（简单的 JSON 文件，便于 web 调整）
_THRESH_FILE = config.DATA_ROOT / "thresholds.json"


def _load_thresholds() -> dict:
    if _THRESH_FILE.exists():
        try:
            return json.loads(_THRESH_FILE.read_text("utf-8"))
        except Exception:
            pass
    return dict(config.DEFAULT_THRESHOLDS_US)


def _save_thresholds(t: dict) -> None:
    _THRESH_FILE.write_text(json.dumps(t, ensure_ascii=False, indent=2), "utf-8")


def serve(host: str = config.DEFAULT_HOST, port: int = config.DEFAULT_PORT,
          open_browser: bool = True) -> None:
    db.init_db()
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"EGO Courier 已启动: {url}  (Ctrl+C 停止)")
    print(f"数据库: {config.DB_PATH}")
    if open_browser and host in ("127.0.0.1", "localhost"):
        # 服务起来后自动打开浏览器（后台线程，延迟一下确保已监听）
        import threading, webbrowser

        def _open():
            import time
            time.sleep(1.0)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=_open, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="EGO Courier Web 服务")
    ap.add_argument("--host", default=config.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    args = ap.parse_args()
    serve(args.host, args.port)
