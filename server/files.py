# -*- coding: utf-8 -*-
"""
文件列表 / 缩略图 / 磁盘用量。
list_files / get_file_dates / _safe_path / get_or_make_thumbnail /
_probe_duration / get_or_make_thumbs / human_size / disk_usage / _walk_mp4。
"""
import os
import re
import logging
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

from . import config
from .config import log, THUMB_DIR, PB_THUMB_DIR


def _walk_mp4(root: Path):
    """遍历 root 下所有 *.mp4,跟随符号链接。yield (rel_path, full_path, size)。"""
    try:
        root_real = root.resolve()
    except (OSError, RuntimeError):
        return
    for dirpath, _dirs, files in os.walk(root_real, followlinks=True):
        for fn in files:
            if not fn.endswith(".mp4") or fn.endswith(".tmp.mp4"):
                continue
            full = Path(dirpath) / fn
            try:
                st = full.stat()
                rel = str(full.relative_to(root_real))
            except (OSError, ValueError):
                continue
            yield rel, full, st.st_size

def list_files(dir_path: Path, q: str = "", sort: str = "mtime", order: str = "desc", page: int = 1, page_size: int = 0):
    """列出目录下的 mp4 文件。支持搜索 / 排序 / 分页。
    q: 文件名模糊匹配(不区分大小写)
    sort: 'name' | 'size' | 'mtime' | 'path'
    order: 'asc' | 'desc'
    page: 1-based
    page_size: 0 = 不分页(返回全部), >0 按页返
    """
    if not dir_path.exists():
        return {"exists": False, "items": [], "count": 0}
    items = []
    try:
        for p in dir_path.rglob("*.mp4"):
            try:
                st = p.stat()
                items.append({
                    "path":    str(p.relative_to(dir_path)),
                    "size":    st.st_size,
                    "size_h":  human_size(st.st_size),
                    "mtime":   datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "mtime_ts": int(st.st_mtime),
                })
            except Exception:
                continue
    except Exception as e:
        return {"exists": True, "items": [], "count": 0, "error": str(e)}
    # 过滤
    if q:
        q_lower = q.lower().strip()
        items = [it for it in items if q_lower in it["path"].lower()]
    # 排序
    sort_keys = {
        "name":  lambda x: x["path"].lower(),
        "size":  lambda x: x["size"],
        "mtime": lambda x: x["mtime_ts"],
        "path":  lambda x: x["path"].lower(),
    }
    sk = sort_keys.get(sort, sort_keys["mtime"])
    items.sort(key=sk, reverse=(order == "desc"))
    # 分页
    total = len(items)
    total_size = sum(i["size"] for i in items)
    if page_size and page_size > 0:
        page = max(1, page)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        page_items = items[start:start + page_size]
    else:
        page_items = items
        page = 1
        total_pages = 1
    # 删 mtime_ts(内部用,不返回前端)
    for it in page_items:
        it.pop("mtime_ts", None)
    return {
        "exists": True,
        "items": page_items,
        "count": total,            # 过滤后总数
        "page": page,
        "page_size": page_size if page_size > 0 else total,
        "total_pages": total_pages,
        "total_size": total_size,
        "total_size_h": human_size(total_size),
        "sort": sort,
        "order": order,
        "q": q,
    }

def get_file_dates(dir_path: Path) -> list:
    """返回目录中所有 mp4 文件的日期列表(YYYY-MM-DD),按日期排序。
    解析文件名中的 YYYYMMDD。
    """
    import re as _re
    if not dir_path.exists():
        return []
    dates = set()
    try:
        for p in dir_path.rglob("*.mp4"):
            m = _re.search(r"(\d{4})(\d{2})(\d{2})", p.name)
            if m:
                dates.add(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    except Exception:
        return []
    return sorted(dates)

def _safe_path(base: Path, rel: str):
    """模块级安全路径拼接(防止路径穿越)。"""
    if not rel:
        return None
    if rel.startswith("/") or "\\" in rel or "\x00" in rel:
        return None
    try:
        full = (base / rel).resolve()
        base_r = base.resolve()
        if not (str(full).startswith(str(base_r) + "/") or str(full) == str(base_r)):
            return None
        return full
    except Exception:
        return None

def get_or_make_thumbnail(dir_name: str, file_path: str):
    """获取或生成缩略图。返回 Path 或 None。"""
    import hashlib as _hl
    base = config.INPUT_DIR if dir_name == "input" else config.OUTPUT_DIR
    full = _safe_path(base, file_path)
    if not full or not full.is_file():
        return None
    sub = THUMB_DIR / dir_name
    sub.mkdir(parents=True, exist_ok=True)
    h = _hl.md5(str(full).encode("utf-8", errors="replace")).hexdigest()[:16]
    thumb = sub / f"{h}.jpg"
    try:
        src_mtime = full.stat().st_mtime
    except Exception:
        return None
    # 缓存命中:thumb 存在且比源文件新
    if thumb.exists():
        try:
            if thumb.stat().st_mtime >= src_mtime:
                return thumb
        except Exception:
            pass
    # 抽帧:ffmpeg 从第 1 秒开始抽一帧, 缩放到 320 宽(高度自适应)
    ffmpeg_bin = "/usr/local/bin/ffmpeg-rkmpp" if Path("/usr/local/bin/ffmpeg-rkmpp").exists() else "/usr/bin/ffmpeg"
    try:
        r = subprocess.run(
            [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
             "-ss", "1", "-i", str(full),
             "-frames:v", "1", "-q:v", "5",
             "-vf", "scale=320:-2",
             str(thumb)],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0 and thumb.exists() and thumb.stat().st_size > 0:
            return thumb
        log(f"thumb ffmpeg failed: {r.stderr[:200]}", level=logging.WARNING)
    except subprocess.TimeoutExpired:
        log(f"thumb timeout: {full}", level=logging.WARNING)
    except Exception as e:
        log(f"thumb error: {e}", level=logging.WARNING)
    return None


def _probe_duration(ffmpeg_bin: str, full: Path) -> float:
    """用 ffprobe 取视频时长(秒)。失败返回 0。"""
    ffprobe = ffmpeg_bin.replace("ffmpeg", "ffprobe")
    if not Path(ffprobe).exists():
        ffprobe = "ffprobe"
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(full)],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception as e:
        log(f"ffprobe duration error: {e}", level=logging.WARNING)
        return 0.0


def get_or_make_thumbs(dir_name: str, file_path: str, count: int = 20):
    """生成播放进度条用缩略图。返回 list[ {i, t, url, cached} ]。
    count: 缩略图数量(默认 20), 实际生成多少看 duration。
    缓存到 data/pb_thumbs/<dir>/<hash>_<i>.jpg,源文件 mtime 变化才重抽。
    """
    import hashlib as _hl
    base = config.INPUT_DIR if dir_name == "input" else config.OUTPUT_DIR
    full = _safe_path(base, file_path)
    if not full or not full.is_file():
        return {"duration": 0.0, "thumbs": []}

    count = max(4, min(int(count), 60))  # 限制 4-60 张

    ffmpeg_bin = "/usr/local/bin/ffmpeg-rkmpp" if Path("/usr/local/bin/ffmpeg-rkmpp").exists() else "/usr/bin/ffmpeg"
    duration = _probe_duration(ffmpeg_bin, full)
    if duration <= 0:
        return {"duration": 0.0, "thumbs": []}

    sub = PB_THUMB_DIR / dir_name
    sub.mkdir(parents=True, exist_ok=True)
    h = _hl.md5(str(full).encode("utf-8", errors="replace")).hexdigest()[:16]

    # 计算均匀分布的时间点,跳过首尾各 1s(避免黑帧/录制结束画面)
    usable = max(1.0, duration - 2.0)
    times = []
    if count == 1:
        times = [1.0 + usable / 2]
    else:
        step = usable / (count - 1)
        times = [1.0 + i * step for i in range(count)]

    try:
        src_mtime = full.stat().st_mtime
    except Exception:
        src_mtime = 0

    thumbs = []
    for i, t in enumerate(times):
        thumb_path = sub / f"{h}_{i}.jpg"
        cached = False
        if thumb_path.exists():
            try:
                if thumb_path.stat().st_mtime >= src_mtime and thumb_path.stat().st_size > 0:
                    cached = True
            except Exception:
                pass

        if not cached:
            try:
                r = subprocess.run(
                    [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                     "-ss", f"{t:.2f}", "-i", str(full),
                     "-frames:v", "1", "-q:v", "6",
                     "-vf", "scale=160:-2",
                     str(thumb_path)],
                    capture_output=True, text=True, timeout=15,
                )
                if r.returncode != 0 or not thumb_path.exists() or thumb_path.stat().st_size == 0:
                    log(f"pb-thumb ffmpeg failed (i={i} t={t:.1f}): {r.stderr[:200]}", level=logging.WARNING)
                    continue
            except subprocess.TimeoutExpired:
                log(f"pb-thumb timeout (i={i})", level=logging.WARNING)
                continue
            except Exception as e:
                log(f"pb-thumb error (i={i}): {e}", level=logging.WARNING)
                continue

        thumbs.append({
            "i": i,
            "t": round(t, 2),
            "url": f"/api/pb/thumb?dir={dir_name}&h={h}&i={i}",
            "cached": cached,
        })

    return {"duration": round(duration, 2), "thumbs": thumbs}

def human_size(n):
    for u in ["B","K","M","G","T"]:
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}P"

def disk_usage():
    out = {}
    for label, p in [("input", config.INPUT_DIR), ("output", config.OUTPUT_DIR), ("scripts", config.SCRIPT_PATH.parent)]:
        try:
            st = os.statvfs(str(p))
            total = st.f_blocks * st.f_frsize
            free  = st.f_bavail * st.f_frsize
            used  = total - free
            out[label] = {
                "total": total, "used": used, "free": free,
                "total_h": human_size(total), "used_h": human_size(used), "free_h": human_size(free),
                "percent": round(used * 100 / total, 1) if total else 0,
            }
        except Exception as e:
            out[label] = {"error": str(e)}
    return out
