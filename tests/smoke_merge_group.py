import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\EGO中转站")
from PySide6.QtWidgets import QApplication

app = QApplication([])
from ego_gui.device_tab import DeviceTab
t = DeviceTab(lambda m: None)

# 旧的 EGOViewer 分组已删除：不再有本地分类勾选/计数、根目录、本地输出、勾选导出按钮
for attr in ("_pack_checks", "_pack_counts", "cmb_ev_root", "edit_local",
             "chk_import", "chk_report", "btn_pack"):
    assert not hasattr(t, attr), "removed attr still present: " + attr

# 新分组必备控件
for attr in ("btn_export", "btn_stop", "edit_dp_ip", "edit_dp_user", "edit_dp_pwd",
             "edit_dp_dirs", "chk_dp_import", "chk_dp_report", "_dp_checks"):
    assert hasattr(t, attr), "missing attr: " + attr

# 三类分类均存在且默认不勾选
import ego_relay.evlog_pack as ep
keys = [c["key"] for c in ep.CATEGORIES]
assert keys == ["app", "sdk", "firmware"], keys
assert set(t._dp_checks) == set(keys), set(t._dp_checks)
assert not any(c.isChecked() for c in t._dp_checks.values()), "categories must not default-checked"

print("merge-group OK; one-click category keys =", keys)
print("MERGE-GROUP OK")
