import sys, socket, subprocess, os, time
sys.path.insert(0, r"D:\EGOCourier")

print("=== 1) 模块导入 ===")
try:
    import ego_relay.api
    print("  import ego_relay.api: OK")
except Exception as e:
    print("  import FAILED:", repr(e))
    import traceback; traceback.print_exc()

print("=== 2) 端口 8360 占用检查 ===")
def port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()
print("  8360 in use:", port_in_use(8360))

print("=== 3) 试启动 server（3 秒后停）===")
env = dict(os.environ)
env["PYTHONPATH"] = r"D:\EGOCourier"
p = subprocess.Popen([sys.executable, "-m", "ego_relay.api", "--port", "8361"],
                     cwd=r"D:\EGOCourier", env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(3)
alive = p.poll() is None
print("  server alive after 3s:", alive)
if not alive:
    out = p.communicate()[0]
    print("  --- server stderr/stdout ---")
    print(out)
else:
    print("  8361 in use (server listening):", port_in_use(8361))
    p.terminate()
