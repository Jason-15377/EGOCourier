"""离屏冒烟：②分组里的「手机日志」勾选能扫描、一键导出能带出手机日志。"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\EGOCourier")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QMessageBox  # noqa: E402

from ego_gui.device_tab import DeviceTab  # noqa: E402

# 离屏环境下模态框无人点击会挂住，统一自动关掉。
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)

app = QApplication([])
tab = DeviceTab(lambda m: print("[log]", m))
tab.show()

# ③分组已合并进②：页面只应有两组
from PySide6.QtWidgets import QGroupBox  # noqa: E402

groups = [g.title() for g in tab.findChildren(QGroupBox)]
print("groups:", groups)
assert not any("③" in t for t in groups), "③ 分组应已删除"
assert any("②" in t for t in groups), "② 分组应保留"

# 1) 勾选手机日志 → 自动扫描
tab.chk_mobile.setChecked(True)
loop = QEventLoop()
if tab._mobile_scan:
    tab._mobile_scan.finished.connect(loop.quit)
    QTimer.singleShot(20000, loop.quit)
    loop.exec()
print("phone in combo:", tab.cmb_mobile.count(), "| state:", tab.lbl_mobile_state.text())
assert tab.cmb_mobile.count() > 0, "没有扫描到已授权手机"

# 2) 只勾手机日志，走一键导出
for chk in tab._pack_checks.values():
    chk.setChecked(False)
out = tempfile.mkdtemp(prefix="merge_smoke_")
tab.edit_local.setText(out)

loop2 = QEventLoop()
tab._start_export()
assert tab._export_worker is not None, "一键导出未启动"
tab._export_worker.finished.connect(loop2.quit)
QTimer.singleShot(180000, loop2.quit)
loop2.exec()

from ego_relay import mobile_adb
mobile_dir = None
for dirpath, dirs, files in os.walk(out):
    if os.path.basename(dirpath) == mobile_adb.PHONE_DIR:
        mobile_dir = dirpath
        break
assert mobile_dir, f"导出目录下没有 {mobile_adb.PHONE_DIR}/：{out}"
phone_dirs = os.listdir(mobile_dir)
print("手机端:", phone_dirs)
assert phone_dirs, "手机端/ 下没有手机目录"
dev = os.path.join(mobile_dir, phone_dirs[0])
for name in ("手机导出包.zip", "sdk日志", "固件日志"):
    p = os.path.join(dev, name)
    if os.path.isfile(p):
        print(f"  {name}: OK ({os.path.getsize(p)} bytes)")
    elif os.path.isdir(p):
        n = sum(len(f) for _d, _s, f in os.walk(p))
        print(f"  {name}/: {n} 个文件")
    else:
        print(f"  {name}: 缺失")
print("OK")
