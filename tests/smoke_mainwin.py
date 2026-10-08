import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\EGO中转站")
from PySide6.QtWidgets import QApplication

app = QApplication([])
from ego_gui.app import MainWindow
w = MainWindow()
print("tabs:", [w.tabs.tabText(i) for i in range(w.tabs.count())])
dt = w.device_tab
print("远端一键导出 group present:", hasattr(dt, "_dp_checks"))
print("EGOViewer/old attrs removed:", not hasattr(dt, "_pack_checks"),
      not hasattr(dt, "btn_pack"), not hasattr(dt, "edit_dp_local"),
      not hasattr(dt, "btn_dp_pull"), not hasattr(dt, "chk_dp_zip"),
      not hasattr(dt, "cmb_ev_root"), not hasattr(dt, "edit_local"))
print("export button:", dt.btn_export.text())
print("FULL-GUI OK")
