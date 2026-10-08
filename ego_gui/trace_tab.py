"""Trace 查询 Tab：输入 trace_id → 关联会话 + 帧表（与 Web Trace 页一致）。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QLabel, QFrame,
)

from ego_relay import db


class TraceTab(QWidget):
    def __init__(self, log):
        super().__init__()
        self.log = log
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        row = QHBoxLayout()
        self.trace_input = QLineEdit()
        self.trace_input.setPlaceholderText("输入 trace_id（32hex 或 sessionId UUID）")
        self.trace_input.returnPressed.connect(self._search)
        self.btn_trace = QPushButton("查询")
        self.btn_trace.setProperty("role", "primary")
        self.btn_trace.clicked.connect(self._search)
        row.addWidget(self.trace_input, 1)
        row.addWidget(self.btn_trace)
        root.addLayout(row)

        # 关联会话（KPI 卡片式，仿 Web）
        self.kpis = QHBoxLayout()
        self.kpi_sessions = self._kpi_card(self.kpis, "关联会话")
        self.kpi_frames = self._kpi_card(self.kpis, "帧")
        root.addLayout(self.kpis)

        root.addWidget(QLabel("关联会话"))
        self.sess_table = QTableWidget(0, 4)
        self.sess_table.setHorizontalHeaderLabels(["会话 ID", "名称", "帧", "异常"])
        self.sess_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.sess_table.setAlternatingRowColors(True)
        root.addWidget(self.sess_table)

        root.addWidget(QLabel("帧"))
        self.trace_table = QTableWidget(0, 6)
        self.trace_table.setHorizontalHeaderLabels(
            ["帧", "侧", "hw_ptp(us)", "sei(us)", "app_recv(us)", "会话"])
        self.trace_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.trace_table.setAlternatingRowColors(True)
        root.addWidget(self.trace_table, stretch=1)

    def _kpi_card(self, layout, label):
        card = QFrame(); card.setObjectName("statCard")
        v = QVBoxLayout(card); v.setContentsMargins(12, 8, 12, 8)
        val = QLabel("0"); val.setObjectName("statValue")
        cap = QLabel(label); cap.setObjectName("statLabel")
        v.addWidget(val); v.addWidget(cap)
        layout.addWidget(card)
        return val

    def _search(self):
        tid = self.trace_input.text().strip()
        if not tid:
            return
        # 关联会话
        rows = db.query(
            "SELECT DISTINCT session_id FROM frame_entries WHERE trace_id=? "
            "UNION SELECT session_id FROM trace_sessions WHERE trace_id=?",
            (tid, tid))
        sids = [r["session_id"] for r in rows]
        self.sess_table.setRowCount(0)
        for sid in sids:
            s = db.get_session(sid)
            r = self.sess_table.rowCount()
            self.sess_table.insertRow(r)
            self.sess_table.setItem(r, 0, QTableWidgetItem(str(sid)))
            self.sess_table.setItem(r, 1, QTableWidgetItem((s or {}).get("name", "") or "-"))
            self.sess_table.setItem(r, 2, QTableWidgetItem(str((s or {}).get("frames", ""))))
            self.sess_table.setItem(r, 3, QTableWidgetItem(str((s or {}).get("anomalies", ""))))

        # 帧
        frames = []
        if sids:
            frames = db.query(
                "SELECT * FROM frame_entries WHERE trace_id=? ORDER BY frame_index LIMIT 2000",
                (tid,))
        self.trace_table.setRowCount(0)
        for f in frames:
            r = self.trace_table.rowCount()
            self.trace_table.insertRow(r)
            vals = [f["frame_index"], f["side"], f["hw_ptp_us"] or "", f["sei_hw_ptp_us"] or "",
                    f["app_recv_us"] or "", f["session_id"]]
            for c, v in enumerate(vals):
                self.trace_table.setItem(r, c, QTableWidgetItem(str(v)))

        self.kpi_sessions.setText(str(len(sids)))
        self.kpi_frames.setText(str(len(frames)))
        self.log(f"Trace {tid[:16]}… 关联会话 {len(sids)}，命中 {len(frames)} 帧")
