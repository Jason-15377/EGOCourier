import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\EGO中转站")
from PySide6.QtWidgets import QApplication

app = QApplication([])
from ego_gui.device_tab import DeviceTab
t = DeviceTab(lambda m: None)
for attr in ("edit_dp_ip", "edit_dp_user", "edit_dp_pwd", "edit_dp_dirs",
             "chk_dp_import", "chk_dp_report"):
    assert hasattr(t, attr), attr
# 输出目录输入框与独立“开始拉取”按钮已删除（由全局「开始一键导出」触发，统一走默认导出目录）
for attr in ("edit_dp_local", "btn_dp_pull", "chk_dp_zip"):
    assert not hasattr(t, attr), "removed attr still present: " + attr
print("SSH pull UI present; ip=%r dirs=%r" % (t.edit_dp_ip.text(), t.edit_dp_dirs.text()))
# 配网成功应自动回填 IP
t._on_provision_done("10.9.82.9")
assert t.edit_dp_ip.text() == "10.9.82.9", t.edit_dp_ip.text()
print("provision->ip autofill OK")
print("GUI-SMOKE OK")
