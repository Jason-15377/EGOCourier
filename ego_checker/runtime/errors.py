"""Short, non-empty diagnostics for user-facing failure paths."""
import errno


def describe_exception(exc):
    """Keep the exception type even when str(exc) is empty (e.g. MemoryError)."""
    name = type(exc).__name__
    try:
        detail = str(exc).strip()
    except Exception:
        detail = "异常说明无法读取"
    if isinstance(exc, MemoryError):
        hint = "内存不足，当前操作无法继续。请关闭占用较多内存的程序，或缩小单次分析的数据范围后重试。"
    elif isinstance(exc, PermissionError):
        hint = "访问被拒绝，请检查相关文件和目录的读写权限。"
    elif isinstance(exc, FileNotFoundError):
        hint = "所需文件或目录不存在，请核对路径。"
    elif isinstance(exc, TimeoutError):
        hint = "操作超时，请检查数据所在磁盘或外部程序的状态。"
    elif isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        hint = "磁盘空间不足，请释放输出磁盘空间后重试。"
    else:
        hint = "" if detail else "异常未提供详细说明，请查看运行日志。"
    reason = "；".join(value for value in (hint, detail) if value)
    return f"{name}：{reason}"


def failure_message(exc, stage):
    return f"发生阶段：{stage or '分析任务'}\n错误：{describe_exception(exc)}"
