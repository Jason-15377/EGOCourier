"""Web API 冒烟测试：启动服务（或连接已在跑的）后跑各端点。"""
import json, urllib.request as u

BASE = "http://127.0.0.1:8360"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = u.Request(BASE + path, data=data, method=method,
                  headers={"Content-Type": "application/json"} if data else {})
    try:
        with u.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read()
    except Exception as e:
        return "ERR", str(e).encode()


def main():
    for p in ["/api/health", "/", "/static/app.js", "/static/style.css", "/api/sessions"]:
        st, b = req("GET", p)
        print(f"GET {p:24} -> {st}  {b[:70]!r}")

    st, b = req("GET", "/api/sessions")
    d = json.loads(b)
    print("\nsessions:", len(d["sessions"]))
    for s in d["sessions"][:3]:
        print(f"  #{s['id']} {s['name'][:45]:45} frames={s['frames']} anom={s['anomalies']}")

    # 详情 / 帧 / 异常 / 导出
    sid = d["sessions"][0]["id"]
    st, b = req("GET", f"/api/sessions/{sid}")
    print(f"\nsession {sid} detail: {st}")
    st, b = req("GET", f"/api/sessions/{sid}/frames?limit=5")
    fr = json.loads(b)
    print(f"frames: total={fr['total']}, first={fr['frames'][0] if fr['frames'] else None}")
    st, b = req("GET", f"/api/sessions/{sid}/anomalies")
    print("anomalies:", json.loads(b)["anomalies"])
    st, b = req("GET", f"/api/sessions/{sid}/export")
    print(f"export zip: status={st}, len={len(b)} bytes, magic={b[:2]!r}")

    # 导入（本地路径）
    st, b = req("POST", "/api/import", {"path": r"D:\Orbbec\测试项目\EGO数采\EGO2615双目\数据\9.15\EGO_AK8896100BJ_20260915_143309"})
    print(f"\nimport: {st}")
    try:
        d = json.loads(b)
        print("  new session:", d["session"]["id"], d["session"]["name"][:40], "frames", d["stats"]["frames"])
    except Exception as e:
        print("  parse err", e, b[:300])


if __name__ == "__main__":
    main()
