import json, urllib.request as u
BASE = "http://127.0.0.1:8362"
def get(p):
    with u.urlopen(BASE + p, timeout=10) as r:
        return r.status, r.read()
for p in ["/api/health", "/", "/static/app.js", "/api/wifi/scan", "/api/sessions"]:
    st, b = get(p)
    print(f"GET {p:20} -> {st}  {b[:60]!r}")
st, b = get("/api/wifi/scan")
d = json.loads(b)
print("wifi networks:", len(d.get("networks", [])), "| interface:", d.get("interface"))
