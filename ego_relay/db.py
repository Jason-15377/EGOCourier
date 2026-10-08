"""SQLite 存储层（纯标准库 sqlite3）。

表：
  devices        设备
  sessions       采集会话（一次导入 = 一个会话；同一会话共用 trace_id）
  log_files      会话内各来源文件
  frame_entries  核心对齐表：每 (side, frame_index) 一行，聚合多来源时间戳
  anomalies      异常记录（按规则）
  trace_sessions trace_id ↔ session 关联
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config

_local = threading.local()
_write_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(str(config.DB_PATH), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def now_us() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000)


def init_db() -> None:
    c = _conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial TEXT UNIQUE,
            name TEXT,
            type TEXT,
            created_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER REFERENCES devices(id),
            name TEXT,
            source_path TEXT,
            trace_id TEXT,
            start_us INTEGER,
            end_us INTEGER,
            status TEXT DEFAULT 'ok',
            meta TEXT,
            created_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS log_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER REFERENCES sessions(id),
            source_type TEXT,
            side TEXT,
            file_path TEXT,
            parsed INTEGER DEFAULT 0,
            count INTEGER DEFAULT 0,
            note TEXT
        );

        CREATE TABLE IF NOT EXISTS frame_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            side TEXT,
            frame_index INTEGER,
            hw_ptp_us INTEGER,
            sei_hw_ptp_us INTEGER,
            app_recv_us INTEGER,
            imu_us INTEGER,
            trace_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_frames_session ON frame_entries(session_id, side, frame_index);

        CREATE TABLE IF NOT EXISTS anomalies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            rule TEXT,
            frame_index INTEGER,
            side TEXT,
            ts_a_us INTEGER,
            ts_b_us INTEGER,
            delta_us INTEGER,
            threshold_us INTEGER,
            severity TEXT,
            detail TEXT,
            frame_image TEXT,
            created_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_anom_session ON anomalies(session_id, rule);

        CREATE TABLE IF NOT EXISTS trace_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT,
            session_id INTEGER REFERENCES sessions(id)
        );
        """
    )
    c.commit()
    migrate()


def migrate() -> None:
    """旧库迁移：补齐新增列（anomalies.frame_image）。"""
    cols = {r["name"] for r in query("PRAGMA table_info(anomalies)")}
    if "frame_image" not in cols:
        exec_write("ALTER TABLE anomalies ADD COLUMN frame_image TEXT")


def exec_write(sql: str, params: tuple = ()) -> int:
    with _write_lock:
        c = _conn()
        cur = c.execute(sql, params)
        c.commit()
        return cur.lastrowid


def exec_many(sql: str, rows: List[tuple]) -> None:
    with _write_lock:
        c = _conn()
        c.executemany(sql, rows)
        c.commit()


def query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    c = _conn()
    return [dict(r) for r in c.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    c = _conn()
    r = c.execute(sql, params).fetchone()
    return dict(r) if r else None


def reset_session(session_id: int) -> None:
    """清空一个会话的帧/异常/文件，用于重新解析。"""
    exec_write("DELETE FROM frame_entries WHERE session_id=?", (session_id,))
    exec_write("DELETE FROM anomalies WHERE session_id=?", (session_id,))
    exec_write("DELETE FROM log_files WHERE session_id=?", (session_id,))


def find_session_by_source(source_path: str) -> Optional[int]:
    """按来源路径查已有会话（用于同一日志/目录只保留一条记录）。"""
    r = query_one("SELECT id FROM sessions WHERE source_path=?", (source_path,))
    return r["id"] if r else None


def delete_session(session_id: int) -> None:
    """级联删除一个会话及其帧/异常/文件/关联。"""
    exec_write("DELETE FROM frame_entries WHERE session_id=?", (session_id,))
    exec_write("DELETE FROM anomalies WHERE session_id=?", (session_id,))
    exec_write("DELETE FROM log_files WHERE session_id=?", (session_id,))
    exec_write("DELETE FROM trace_sessions WHERE session_id=?", (session_id,))
    exec_write("DELETE FROM sessions WHERE id=?", (session_id,))


# ---------------------------------------------------------------------------
# 便捷写/读
# ---------------------------------------------------------------------------
def create_device(serial: str, name: str = "", dev_type: str = "EGO2615双目") -> int:
    dev = query_one("SELECT id FROM devices WHERE serial=?", (serial,))
    if dev:
        return dev["id"]
    return exec_write(
        "INSERT INTO devices(serial,name,type,created_at) VALUES(?,?,?,?)",
        (serial, name, dev_type, now_us()),
    )


def create_session(device_id: Optional[int], name: str, source_path: str,
                   trace_id: Optional[str], start_us: Optional[int],
                   end_us: Optional[int], meta: dict) -> int:
    return exec_write(
        "INSERT INTO sessions(device_id,name,source_path,trace_id,start_us,end_us,status,meta,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (device_id, name, source_path, trace_id, start_us, end_us, "ok",
         json.dumps(meta, ensure_ascii=False), now_us()),
    )


def get_session(session_id: int) -> Optional[Dict[str, Any]]:
    s = query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    if s and s["meta"]:
        s["meta"] = json.loads(s["meta"])
    return s


def update_session_meta(session_id: int, **fields: Any) -> None:
    """向会话 meta 追加/覆盖字段（如 source_sig）。"""
    s = query_one("SELECT meta FROM sessions WHERE id=?", (session_id,))
    meta = json.loads(s["meta"]) if s and s["meta"] else {}
    meta.update(fields)
    exec_write("UPDATE sessions SET meta=? WHERE id=?",
               (json.dumps(meta, ensure_ascii=False), session_id))


def list_sessions(limit: int = 200) -> List[Dict[str, Any]]:
    return query(
        "SELECT s.*, d.serial AS device_serial, "
        "(SELECT COUNT(*) FROM frame_entries f WHERE f.session_id=s.id) AS frames, "
        "(SELECT COUNT(*) FROM anomalies a WHERE a.session_id=s.id) AS anomalies "
        "FROM sessions s LEFT JOIN devices d ON d.id=s.device_id "
        "ORDER BY s.id DESC LIMIT ?",
        (limit,),
    )


def session_frames(session_id: int, side: Optional[str] = None,
                   frame_index: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM frame_entries WHERE session_id=?"
    params: list = [session_id]
    if side:
        sql += " AND side=?"
        params.append(side)
    if frame_index is not None:
        sql += " AND frame_index=?"
        params.append(frame_index)
    sql += " ORDER BY frame_index"
    return query(sql, tuple(params))


def session_anomalies(session_id: int, rule: Optional[str] = None,
                      severity: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM anomalies WHERE session_id=?"
    params: list = [session_id]
    if rule:
        sql += " AND rule=?"
        params.append(rule)
    if severity:
        sql += " AND severity=?"
        params.append(severity)
    sql += " ORDER BY rule, frame_index"
    return query(sql, tuple(params))


def session_stats(session_id: int) -> Dict[str, Any]:
    total = query_one(
        "SELECT COUNT(*) AS n FROM frame_entries WHERE session_id=?", (session_id,))["n"]
    n_anom = query_one(
        "SELECT COUNT(*) AS n FROM anomalies WHERE session_id=?", (session_id,))["n"]
    by_rule = query(
        "SELECT rule, COUNT(*) AS n, MAX(delta_us) AS max_delta "
        "FROM anomalies WHERE session_id=? GROUP BY rule", (session_id,))
    by_side = query(
        "SELECT side, COUNT(*) AS n FROM frame_entries WHERE session_id=? GROUP BY side",
        (session_id,))
    return {"frames": total, "anomalies": n_anom,
            "anomalies_by_rule": by_rule, "frames_by_side": by_side}
