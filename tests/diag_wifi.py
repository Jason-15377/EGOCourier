import sys
sys.path.insert(0, r"D:\EGOCourier")
from ego_relay import wifi
import locale
print("preferred encoding:", locale.getpreferredencoding(False))
out = wifi._run("show", "interfaces")
print("--- interfaces raw (repr of each line) ---")
for ln in out.splitlines():
    if ln.strip():
        print(repr(ln))
