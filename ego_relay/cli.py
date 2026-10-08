"""EGO Courier 命令行工具（`python -m ego_relay.cli` 或打包后的 `ego`）。

子命令：
  ego import <path>           导入本地目录/zip 并解析、对齐、算异常
  ego list                    列出会话
  ego show <session_id>       会话统计 + 异常汇总
  ego query --trace X | --session N [--side L|R] [--anomaly-only]  查询帧/异常
  ego export <session_id> [-o out.zip] [--trace X]  一键打包导出
  ego thresholds [show|set key=us ...]  查看/修改异常阈值
  ego server [--host H] [--port N]     启动 Web 服务
  ego status                 健康检查
  ego device pull <...>      预留：WiFi/网络导出设备日志（通道未配置）
"""

from __future__ import annotations

import argparse
import os
import sys

from . import db, config


def _human(us):
    if not us:
        return "-"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S.%f")[:-3]


def cmd_import(args):
    from . import parsers, analysis
    ps = parsers.import_path(args.path)
    thr = dict(config.DEFAULT_THRESHOLDS_US)
    for kv in args.threshold or []:
        k, v = kv.split("=", 1)
        thr[k.strip()] = int(float(v)) * 1000 if k.strip() in ("app_receive_delay",) else int(float(v))
    r = analysis.align_and_analyze(ps, thr)
    print(f"导入完成: 会话 #{r['session_id']}  {ps.name}")
    print(f"  设备: {ps.device_serial or '-'}   trace_id: {ps.trace_id or '-'}")
    print(f"  时间范围: {_human(r['start_us'])} ~ {_human(r['end_us'])}")
    print(f"  对齐帧: {r['frames']}   异常: {r['anomalies']}")
    for f in ps.files:
        flag = "OK " if f.get("parsed") else "ERR"
        print(f"    [{flag}] {f['source_type']:12} {f.get('side','-'):6} {f['count']:>6}  {f['file']}")


def cmd_list(args):
    rows = db.list_sessions(500)
    if not rows:
        print("（暂无会话）")
        return
    print(f"{'ID':<5}{'设备':<14}{'帧':<8}{'异常':<6}{'trace_id':<34}名称")
    for s in rows:
        print(f"{s['id']:<5}{str(s.get('device_serial') or '-'):<14}{s['frames']:<8}"
              f"{s['anomalies']:<6}{(s.get('trace_id') or '')[:32]:<34}{s['name']}")


def cmd_show(args):
    s = db.get_session(args.session_id)
    if not s:
        print(f"会话 {args.session_id} 不存在")
        return 1
    st = db.session_stats(s["id"])
    print(f"会话 #{s['id']}: {s['name']}  (设备 {s.get('device_serial') or '-'})")
    print(f"  trace_id: {s.get('trace_id') or '-'}")
    print(f"  时间范围: {_human(s.get('start_us'))} ~ {_human(s.get('end_us'))}")
    sides = ", ".join("{}={}".format(x["side"], x["n"]) for x in st["frames_by_side"])
    print("  对齐帧: {}  ({})".format(st["frames"], sides))
    print("  异常: ", st['anomalies_by_rule'] or "无")
    if s.get("meta", {}).get("files"):
        print("  来源文件:")
        for f in s["meta"]["files"]:
            print(f"    {f['source_type']:12} {f.get('side','-'):6} {f['count']:>6}  {f['file']}")


def cmd_query(args):
    s = db.get_session(args.session_id)
    if not s:
        print(f"会话 {args.session_id} 不存在")
        return 1
    if args.anomalies:
        anoms = db.session_anomalies(args.session_id, rule=args.rule, severity=args.severity)
        print(f"{'规则':<20}{'帧':<8}{'侧':<10}{'偏差us':<10}{'阈值us':<10}{'级别'}")
        for a in anoms:
            print(f"{a['rule']:<20}{a['frame_index']:<8}{a['side']:<10}{a['delta_us']:<10}"
                  f"{a['threshold_us']:<10}{a['severity']}")
        print(f"共 {len(anoms)} 条异常")
        return
    frames = db.session_frames(args.session_id, side=args.side)
    if args.anomaly_only:
        keys = {(a["frame_index"], a["side"]) for a in db.session_anomalies(args.session_id)
                if a["side"] != "frame"}
        fids = {a["frame_index"] for a in db.session_anomalies(args.session_id) if a["side"] == "frame"}
        frames = [f for f in frames if (f["frame_index"], f["side"]) in keys or f["frame_index"] in fids]
    print(f"{'帧':<6}{'侧':<6}{'hw_ptp(us)':<18}{'hw_ptp本地':<24}{'sei(us)':<16}{'app_recv(us)':<16}{'imu(us)':<16}")
    for f in frames[:args.limit]:
        print(f"{f['frame_index']:<6}{f['side']:<6}{str(f['hw_ptp_us'] or ''):<18}{_human(f['hw_ptp_us']):<24}"
              f"{str(f['sei_hw_ptp_us'] or ''):<16}{str(f['app_recv_us'] or ''):<16}{str(f['imu_us'] or ''):<16}")
    print(f"共 {len(frames)} 帧")


def cmd_export(args):
    from . import export
    p = export.export_session(args.session_id, out_path=args.output, trace_id=args.trace)
    print(f"已导出: {p}")


def cmd_attach(args):
    from . import analysis
    s = db.get_session(args.session_id)
    if not s:
        print(f"会话 {args.session_id} 不存在")
        return 1
    r = analysis.attach_app_log(args.session_id, args.app_log)
    print(f"已把 app 日志关联进会话 #{args.session_id} 并重算")
    print(f"  trace_id: {r.get('trace_id') or '-'}")
    print(f"  对齐帧: {r['frames']}   异常: {r['anomalies']}")
    print(f"  阈值: {r['thresholds_us']}")


def cmd_thresholds(args):
    if args.action == "show" or args.action is None:
        for k, v in config.load_thresholds().items():
            print(f"{k:<22} {v:>8} us  ({v/1000:g} ms)")
        return
    if args.action == "set":
        cur = config.load_thresholds()
        for kv in args.kv:
            k, v = kv.split("=", 1)
            if k not in config.DEFAULT_THRESHOLDS_US:
                print(f"未知规则: {k}（可选: {list(config.DEFAULT_THRESHOLDS_US)}）")
                return 1
            cur[k] = int(float(v))
        config.save_thresholds(cur)
        print("已更新:")
        for k, v in config.load_thresholds().items():
            print(f"  {k:<22} {v:>8} us")
    else:
        print(f"未知操作: {args.action}")
        return 1


def cmd_status(args):
    from . import __appname__, __version__
    db.init_db()
    print(f"{__appname__} {__version__}   状态: OK")
    print(f"数据库: {config.DB_PATH}")
    print(f"会话数: {db.query_one('SELECT COUNT(*) AS n FROM sessions')['n']}")


def cmd_wifi(args):
    from . import wifi
    if args.scan:
        st = wifi.scan_with_status()
        iface = st.get("interface", {})
        print(f"当前连接: {iface.get('ssid') or '-'}  信号 {iface.get('signal', 0)}%  "
              f"{'已连接' if iface.get('connected') else '未连接'}")
        nets = st.get("networks", [])
        print(f"{'SSID':<28}{'信号':<6}{'安全类型':<20}{'状态'}")
        for n in nets:
            state = "●已连接" if n.get("connected") else ""
            print(f"{n['ssid'][:26]:<28}{str(n['signal'])+'%':<6}{n['security'][:18]:<20}{state}")
        if not nets:
            print("（未发现可见 WiFi，或无无线网卡）")
        return
    print(wifi.status())


def cmd_device(args):
    print("设备导出通道尚未配置（预留）。")
    print("已提供基础能力: ego wifi scan（扫描 WiFi / 连接状态）")
    print("规划通道: ADB / SMB 网络共享 / HTTP 自定义接口 —— 待确认设备暴露方式后启用。")
    return 1


def cmd_ble(args):
    from . import ble_provision
    if args.action == "status":
        print("EgoLowBle.dll 可用" if ble_provision.dll_available()
              else "EgoLowBle.dll 不可用（请先安装 EGOViewer）")
        return 0
    if args.action == "scan":
        if not ble_provision.dll_available():
            print("EgoLowBle.dll 不可用，无法扫描")
            return 1
        try:
            devs = ble_provision.scan_devices()
        except ble_provision.BleError as e:
            print(f"扫描失败: {e}")
            return 1
        print(f"{'名称':<32}{'地址'}")
        for d in devs:
            print(f"{(d.get('name') or '-')[:30]:<32}{d.get('address') or '-'}")
        print(f"（共 {len(devs)} 台设备）")
        return 0
    if args.action == "provision":
        if not args.address or not args.ssid:
            print("用法: ego ble provision --address <mac> --ssid <wifi> [--password <pwd>]")
            return 1
        try:
            r = ble_provision.provision(args.address, args.ssid, args.password or "")
        except ble_provision.BleError as e:
            print(f"配网失败: {e}")
            return 1
        if r.get("connected") and r.get("ip"):
            print(f"配网成功，设备 IP = {r['ip']}")
            return 0
        print("配网已下发，但未在超时内拿到 IP")
        return 1
    return 1


def cmd_export_device(args):
    from . import sftp_pull
    host = args.host or config.DEVICE_IP
    user = args.user or config.SSH_USER
    pwd = args.password or config.SSH_PASSWORD
    if not (host and user and pwd):
        print("请提供 SSH 凭证：--host --user --password（或用 config.DEVICE_IP 等）")
        return 1
    dirs = (args.remote_dirs or "/ego/logs").split("&")
    local = args.local_dir or str(config.EXPORT_DIR)
    print(f"从 {host} 拉取 {dirs} → {local}…")
    files = sftp_pull.pull_logs(host, user, pwd, dirs, local,
                                zip_first=not args.no_zip)
    print("已下载:")
    for f in files:
        print("  ", f)
    if args.auto_import:
        imp = sftp_pull.import_pulled(files[0])
        s = imp["session"]
        print(f"自动导入成功: 会话 #{s['id']} {s['name']}（帧 {imp['stats'].get('frames')}）")
    return 0


def cmd_evlog(args):
    from . import evlog_pack
    if args.action == "list":
        roots = evlog_pack.find_ego_viewer_roots()
        if not roots:
            print("未检测到本机 EGOViewer 安装目录")
            return 1
        print("检测到的 EGOViewer 安装根目录:")
        for i, rt in enumerate(roots, 1):
            counts = {k: len(v) for k, v in evlog_pack.collect(rt).items()}
            print(f"  {i}. {rt}")
            print(f"     app={counts['app']}  sdk={counts['sdk']}  firmware={counts['firmware']}")
        return 0
    if args.action == "export":
        root = args.root or (evlog_pack.find_ego_viewer_roots() or [""])[0]
        if not root or not os.path.isdir(root):
            print(f"EGOViewer 根目录无效：{root or '(未检测到)'}（用 --root 指定）")
            return 1
        out_dir = args.output or str(config.EXPORT_DIR)
        days = args.days
        print(f"收集 {root} 的日志（最近 {days} 天）并打包到 {out_dir}…")
        import shutil
        import tempfile
        work = tempfile.mkdtemp(prefix="evlog_")
        try:
            _staged, collected = evlog_pack.stage(
                root, work, max_age_days=days,
                progress_cb=lambda pct: print(f"  收集 {pct}%", end="\r"))
            n = sum(len(v) for v in collected.values())
            if n == 0:
                print(f"\n失败: 最近 {days} 天内未找到日志（可用 --days 调大）")
                return 1
            zip_path = evlog_pack.pack_staged(
                work, out_dir, root, collected, prefix=args.prefix,
                max_age_days=days,
                progress_cb=lambda pct: print(f"  打包 {pct}%", end="\r"))
            print(f"\n已导出: {zip_path}")
            if args.report and (collected.get("app") or collected.get("firmware")):
                from . import export as export_mod, sftp_pull, db
                imp = sftp_pull.import_pulled(work)
                sid = (imp.get("session") or {}).get("id")
                if sid:
                    s = db.get_session(sid)
                    stats = db.session_stats(sid)
                    html = export_mod._html_report(sid, s, stats)
                    name = (s or {}).get("name") or f"session_{sid}"
                    rp = os.path.join(out_dir, f"report_{name}_{sid}.html")
                    with open(rp, "w", encoding="utf-8") as fh:
                        fh.write(html)
                    print(f"分析入库: 会话 #{sid}，报告: {rp}")
                else:
                    print("导入未产生会话，跳过报告")
            return 0
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ego", description="EGO Courier")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("import", help="导入本地目录/zip")
    p.add_argument("path")
    p.add_argument("--threshold", action="append", default=[], help="覆盖阈值 key=us，可多次")
    p.set_defaults(func=cmd_import)

    sub.add_parser("list", help="列出会话").set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="会话详情")
    p.add_argument("session_id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("query", help="查询帧/异常")
    p.add_argument("session_id", type=int)
    p.add_argument("--side", choices=["left", "right"])
    p.add_argument("--anomalies", action="store_true", help="显示异常而非帧")
    p.add_argument("--anomaly-only", action="store_true")
    p.add_argument("--rule")
    p.add_argument("--severity", choices=["error", "warn"])
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("export", help="一键打包导出")
    p.add_argument("session_id", type=int)
    p.add_argument("-o", "--output")
    p.add_argument("--trace")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("attach-log", help="把 EGOViewer app 日志关联进已有会话")
    p.add_argument("session_id", type=int)
    p.add_argument("app_log", help="ego-viewer-*.log 路径")
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("thresholds", help="异常阈值")
    p.add_argument("action", nargs="?", choices=["show", "set"])
    p.add_argument("kv", nargs="*", help="set: key=us ...")
    p.set_defaults(func=cmd_thresholds)

    sub.add_parser("status", help="健康检查").set_defaults(func=cmd_status)

    p = sub.add_parser("wifi", help="WiFi 扫描 / 连接状态")
    p.add_argument("--scan", action="store_true", help="扫描可见 WiFi 列表")
    p.set_defaults(func=cmd_wifi)

    p = sub.add_parser("device", help="设备导出")
    p.add_argument("--host")
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--remote-dirs", default="/ego/logs")
    p.add_argument("--local-dir")
    p.add_argument("--no-zip", action="store_true")
    p.add_argument("--no-import", dest="auto_import", action="store_false", default=True)
    p.set_defaults(func=cmd_export_device)

    p = sub.add_parser("ble", help="BLE 配网")
    p.add_argument("action", choices=["status", "scan", "provision"])
    p.add_argument("--address", help="蓝牙 MAC 地址")
    p.add_argument("--ssid", help="Wi-Fi 名称")
    p.add_argument("--password", default="", help="Wi-Fi 密码（开放网络留空）")
    p.set_defaults(func=cmd_ble)

    p = sub.add_parser("evlog", help="本地 EGOViewer 三类日志收集/打包")
    p.add_argument("action", choices=["list", "export"])
    p.add_argument("--root", help="EGOViewer 安装根目录（export 用；缺省自动检测第一个）")
    p.add_argument("-o", "--output", help="导出保存目录（缺省 data/exports）")
    p.add_argument("--prefix", default="EGOViewer_logs", help="zip 文件名前缀")
    p.add_argument("--days", type=int, default=7, help="只打包最近 N 天内修改的日志")
    p.add_argument("--report", action="store_true", help="打包后自动导入分析并生成 HTML 报告")
    p.set_defaults(func=cmd_evlog)

    args = ap.parse_args(argv)
    db.init_db()
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
