#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# 数采分析工具入口
#   默认启动图形界面; 加 --cli 可用命令行运行 (便于批量/自动化).
#   用法: python main.py --cli <数据路径> [--out <输出路径>] [--wl <白名单文件>]
# ============================================================================
import argparse
import sys


def configure_windows_output():
    # Windows 独立程序重定向中文日志时使用 UTF-8。
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def main():
    # Frozen workers must dispatch before argument parsing, logging or Qt startup.
    import multiprocessing
    multiprocessing.freeze_support()
    configure_windows_output()
    # 命令行模式
    if "--cli" in sys.argv:
        return run_cli(sys.argv[sys.argv.index("--cli") + 1:])
    from ego_checker.runtime.run_log import (
        RunLogWriter, install_python_exception_logging,
    )
    run_log = None
    log_error = ""
    python_exception_hooks = None
    try:
        run_log = RunLogWriter.create()
    except OSError as exc:
        log_error = str(exc)
    if run_log is not None:
        python_exception_hooks = install_python_exception_logging(run_log)
        try:
            run_log.enable_fault_handler()
        except Exception as exc:  # noqa: BLE001 - Python hooks still provide diagnostics
            run_log.write(f"[faulthandler初始化异常] {exc}")
        try:
            run_log.write_runtime_header()
        except Exception as exc:  # noqa: BLE001 - keep startup diagnostics alive
            import traceback
            run_log.write(f"[启动信息记录异常]\n{traceback.format_exc()}")
    from ego_checker.ui.application import main as gui_main
    return gui_main(
        run_log=run_log,
        log_error=log_error,
        python_exception_hooks=python_exception_hooks,
    )


def run_cli(argv):
    from ego_checker.runtime.run_log import AnalysisRunLog
    from ego_checker.runtime.control import AnalysisStopped
    try:
        with AnalysisRunLog() as run_log:
            run_log.write(f'命令行参数: {argv!r}')
            run_log.display(f'本次分析日志: {run_log.path}')
            code = _run_cli(argv, run_log)
            run_log.outcome = 'COMPLETED' if code in (0, 1) else 'FAILED'
            # Invalid input also returns 1, but has not produced any report.
            if not getattr(run_log, 'report_created', False):
                run_log.outcome = 'FAILED'
            run_log.write(f'[CLI退出] exit_code={code}')
            return code
    except (AnalysisStopped, KeyboardInterrupt):
        return 130
    except Exception as exc:
        if sys.stderr is not None:
            print(f'分析或日志保存失败：{type(exc).__name__}: {exc}', file=sys.stderr)
        return 2


def _run_cli(argv, run_log):
    import traceback
    from pathlib import Path
    import ego_checker.app.api as fc
    import ego_checker.reporting.outputs as rep
    from ego_checker.reporting.boundaries import collect_boundary_rows
    from ego_checker.app.analysis_log import event
    from ego_checker.app.parallel import ANALYSIS_WORKER_CHOICES
    print = run_log.display

    ap = argparse.ArgumentParser(description="数采分析工具 (CLI)")
    ap.add_argument("root", help="数据路径 (含 camera / imu 的 CSV)")
    ap.add_argument("--input-mode", choices=("single", "multi"), default="single",
                    help="会话数据类型：single 单设备（默认）；multi 多设备批量父目录")
    ap.add_argument("--layout-mode", choices=("separate", "flat"), default="separate",
                    help="落盘格式：separate 录制独立目录（默认）；"
                         "flat 多次录制平铺（同一设备目录并存多次录制，按会话分别出报告）")
    ap.add_argument("--pairing-level", choices=("meta", "parent"), default="meta",
                    help="多设备配对等级：meta 校验配对（默认，校验UUID/开始时间/主从引用）；"
                         "parent 父目录配对（直接按设备父目录组合，不校验上述条件）")
    ap.add_argument("--out", default="", help="输出路径 (默认 <root>/fsync_output)")
    ap.add_argument("--wl", default="", help="白名单文件")
    ap.add_argument("--analysis-workers", type=int, choices=ANALYSIS_WORKER_CHOICES, default=0,
                    help="分析进程数：0 自动（默认，最多 8）、1 串行、2/4/6/8；按可用内存限流")
    ap.add_argument("--analysis-timeout-s", type=float, default=0,
                    help="每段子进程分析超时秒数，0 不限制；暂停时间不计入")
    ap.add_argument("--interval-ms", type=float, default=10.0)
    ap.add_argument("--row-align-ms", type=float, default=10.0)
    ap.add_argument("--cam-sync-ms", type=float, default=1.0)
    ap.add_argument("--imu-sync-ms", type=float, default=1.0)
    ap.add_argument("--rgb-drop-mult", type=float, default=1.5)
    ap.add_argument("--imu-drop-mult", type=float, default=1.5)
    ap.add_argument("--audio-target-s", type=float, default=None,
                    help="每个音频流的目标时长（秒）；需配合实测 --audio-drift-ppm 才判定")
    ap.add_argument("--audio-drift-ppm", type=float, default=None,
                    help="音频时钟实测漂移绝对值 ppm")
    ap.add_argument("--audio-sha256", action="store_true",
                    help="计算音频媒体文件及配套 CSV 的 SHA256 留证")
    ap.add_argument("--no-audio", dest="audio", action="store_false",
                    help="不测试音频，报告不显示音频相关列")
    ap.add_argument("--no-meta-check", dest="meta_check", action="store_false",
                    help="不检测 meta 元数据完整性/准确性，报告不显示该 section")
    ap.set_defaults(audio=True, meta_check=True)
    args = ap.parse_args(argv)

    import math
    if not math.isfinite(args.analysis_timeout_s) or args.analysis_timeout_s < 0:
        ap.error("--analysis-timeout-s 必须为有限的非负秒数")

    if args.audio_target_s is not None and args.audio_target_s <= 0:
        ap.error("--audio-target-s 必须大于 0")
    if args.audio_drift_ppm is not None and args.audio_drift_ppm < 0:
        ap.error("--audio-drift-ppm 不能为负数")
    if args.audio and args.audio_drift_ppm is not None and args.audio_target_s is None:
        ap.error("--audio-drift-ppm 必须与 --audio-target-s 同时使用")

    root = Path(args.root)
    out = Path(args.out) if args.out else root / "fsync_output"
    run_log.configuration(root, out, vars(args))
    if not root.is_dir():
        print(f"错误: 数据路径不存在 — {root}")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    params = {
        "input_mode": args.input_mode,
        "layout_mode": args.layout_mode,
        "pairing_level": args.pairing_level,
        "analysis_workers": args.analysis_workers,
        "analysis_timeout_s": args.analysis_timeout_s,
        "frame_count_ms": 1.0, "interval_ms": args.interval_ms,
        "row_align_ms": args.row_align_ms, "cam_sync_ms": args.cam_sync_ms,
        "imu_sync_ms": args.imu_sync_ms,
        "rgb_drop_mult": args.rgb_drop_mult, "imu_drop_mult": args.imu_drop_mult,
        "audio": args.audio,
        "meta_check": args.meta_check,
        "audio_target_s": args.audio_target_s,
        "audio_drift_ppm": args.audio_drift_ppm,
        "audio_sha256": args.audio_sha256,
    }
    results, err = fc.run_analysis(root, params, log=run_log.write)
    if err:
        print(f"错误: {err}")
        return 1
    reported_session_warnings = set()
    for result in results:
        for notice in result.notices:
            print(f"NOTICE Part {result.name}: {notice}")
        for warning in result.session_info.get("warnings", []):
            key = (str(result.path), warning)
            if key not in reported_session_warnings:
                print(f"WARNING 会话 {result.path}: {warning}")
                reported_session_warnings.add(key)
        if result.error:
            print(f"ERROR Part {result.name}: {result.error}")
        if result.imu_error:
            print(f"ERROR Part {result.name}: {result.imu_error}；未执行IMU同步、丢帧及多组差值检查")
        for error in result.imu_frequency_validation_errors:
            print(f'ERROR Part {result.name}: IMU频率核对：{error}')
    if args.wl:
        with run_log.stage('处理白名单'):
            fc.apply_whitelist(results, fc.load_whitelist(Path(args.wl)))
    try:
        with run_log.stage('跨段边界测试'):
            boundary_rows = collect_boundary_rows(results, root, params,
                progress=lambda detail, completed, count: run_log.write(
                    f'[阶段进度] 跨段边界测试：{detail}（{completed}/{count}）'))
            event(run_log.write, '边界检查完成', count=len(boundary_rows))
        with run_log.stage('生成合并 HTML 报告'):
            html_path = rep.generate_html(results, rep.unique_path(out / rep.REPORT_NAME),
                                      root, params, boundary_rows=boundary_rows)
    except (OSError, ValueError) as exc:
        run_log.write('[报告异常]\n' + traceback.format_exc())
        print(f"测试报告生成失败：{exc}")
        return 2
    stats = rep.compute_stats(results)
    print(f"Part 总数={stats['total']} 正常={stats['normal']} 异常={stats['abnormal']} "
          f"执行异常={stats['errors']} 未验证={stats['unverified']} "
          f"音频证据未验证(诊断)={stats['audio_unverified']} "
          f"音频节拍疑似异常(诊断)={stats['audio_suspected']} 白名单={stats['whitelisted']}")
    print(f"HTML 报告: {html_path}")
    run_log.report_created = True
    return 2 if stats['errors'] or stats['unverified'] else (1 if stats['abnormal'] else 0)
