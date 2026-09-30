"""分析结果缓存：按来源路径/文件的签名决定是否复用上次结果。

目的：
- 同一目录/zip 未变化时，跳过耗时的 video_analyzer（ffmpeg/MCAP 解析），直接复用缓存，
  提升现场重复分析体验。
- 签名基于相关文件（mp4/mcap/csv/log 等）的路径、大小、mtime 计算，任何变化都会失效。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config

CACHE_DIR = config.DATA_ROOT / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 参与签名的文件后缀（分析涉及的类型）
_RELEVANT = {".mp4", ".mcap", ".csv", ".log", ".txt", ".json", ".yaml", ".yml", ".zip"}


def source_signature(path: str) -> str:
    """计算来源路径的签名。目录递归相关文件；zip 直接用文件本身。"""
    p = Path(path)
    items = []
    try:
        if p.is_file():
            st = p.stat()
            items.append((p.name, st.st_size, int(st.st_mtime)))
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and f.suffix.lower() in _RELEVANT:
                    st = f.stat()
                    items.append((str(f.relative_to(p)), st.st_size, int(st.st_mtime)))
    except OSError:
        pass
    items.sort()
    h = hashlib.sha256()
    for it in items:
        h.update(repr(it).encode("utf-8"))
    return h.hexdigest()


def _video_cache_path(sig: str) -> Path:
    return CACHE_DIR / f"video_{sig[:16]}.json"


def load_video_cache(sig: str) -> dict | None:
    """读取视频分析模型缓存；不存在/损坏返回 None。"""
    fp = _video_cache_path(sig)
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_video_cache(sig: str, video_model: dict) -> None:
    """把视频分析模型写入磁盘缓存。"""
    try:
        _video_cache_path(sig).write_text(
            json.dumps(video_model, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def clear_cache() -> None:
    """清空全部视频缓存（数据目录里 cache/ 下 video_*.json）。"""
    for fp in CACHE_DIR.glob("video_*.json"):
        try:
            fp.unlink()
        except OSError:
            pass
