import json, urllib.request as u, urllib.error
BASE = "http://127.0.0.1:8360"
def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = u.Request(BASE + path, data=data, method=method,
                  headers={"Content-Type": "application/json"} if data else {})
    try:
        with u.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return "ERR", str(e).encode()

for p in ["/api/health", "/", "/api/sessions", "/static/app.js"]:
    st, b = req("GET", p); print(f"GET {p:18} -> {st}")

# attach 端点：非法路径应 404 优雅返回
st, b = req("POST", "/api/sessions/1/attach", {"path": r"C:\nonexistent.log"})
print(f"POST attach (bad path) -> {st}  {b[:60]}")

# 会话详情含 meta
st, b = req("GET", "/api/sessions/1")
print(f"GET session 1 -> {st}")
