#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compress_video.sh 可视化管理 - 后端
Python stdlib http.server, zero deps.
"""
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import signal
import shlex
import configparser
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote as _urlquote
from pathlib import Path

# ============== 配置 ==============
# 默认装在脚本同级目录(env VIDEO_MANAGER_DIR 可覆盖)。这样 install.sh
# 装到 /opt/video-manager 或 /home/x/scripts/video-manager 都能用。
APP_DIR        = Path(os.environ.get("VIDEO_MANAGER_DIR", str(Path(__file__).parent.resolve())))
# 备选回退:老路径(LYD 原装路径),给从老主机上 git pull 同步的便利
if not APP_DIR.exists():
    _legacy = Path("/home/kxrdyf/scripts/video-manager")
    if _legacy.exists():
        APP_DIR = _legacy
DATA_DIR       = APP_DIR / "data"
LOG_DIR        = APP_DIR / "logs"
STATIC_DIR     = APP_DIR / "static"
DB_PATH        = DATA_DIR / "history.db"
APP_LOG_PATH   = LOG_DIR / "app.log"

# 脚本/ofelia 路径可以从 env 调,不设则走默认(新装机不关心这些路径)
_SCRIPT_DIR        = Path(os.environ.get("VIDEO_MANAGER_SCRIPT_DIR",
                                         os.environ.get("VIDEO_MANAGER_DIR",
                                                        "/home/kxrdyf/scripts")))
SCRIPT_PATH    = _SCRIPT_DIR / "compress_video.sh"
SCRIPT_LOG     = _SCRIPT_DIR / "compress.log"
SCRIPT_LOCK    = _SCRIPT_DIR / "compress.lock"
_FFMPEG_DIR    = Path(os.environ.get("VIDEO_MANAGER_FFMPEG_DIR",
                                     "/home/kxrdyf/docker/ffmpeg"))
OFELIA_INI     = _FFMPEG_DIR / "ofelia.ini"
OFELIA_BAK     = _FFMPEG_DIR / "ofelia.ini.bak"

# 默认值（仅 DB 无记录时使用；首次启动会落库，之后可在 UI 配置页修改）
_INPUT_DIR_DEFAULT  = Path(os.environ.get("VIDEO_MANAGER_INPUT_DIR",
                                          "/volume1/Videos/XiaomiCamera_00_B888809C1E93"))
_OUTPUT_DIR_DEFAULT = Path(os.environ.get("VIDEO_MANAGER_OUTPUT_DIR",
                                           "/volume1/Videos/compressed"))
INPUT_DIR  = _INPUT_DIR_DEFAULT
OUTPUT_DIR = _OUTPUT_DIR_DEFAULT

HOST           = "0.0.0.0"
PORT           = 8765

# ============== 工具 ==============
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ============== Logging ==============
# 三路 handler:
#   1. logs/app.log  — 全量详细(level/timestamp/module/lineno) + size 轮转
#   2. /home/kxrdyf/scripts/compress.log — 紧凑 bash 兼容格式(给 UI 读) + size 轮转
#   3. stdout        — 紧凑格式(nohup 重定向到 logs/stdout.log)
LOG_LEVEL_FILE     = logging.DEBUG
LOG_LEVEL_COMPACT  = logging.INFO
LOG_MAX_BYTES      = 2 * 1024 * 1024   # 2 MiB / 文件
LOG_BACKUP_APP     = 5
LOG_BACKUP_COMPACT = 3

_app_handler = RotatingFileHandler(
    APP_LOG_PATH,
    maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_APP,
    encoding="utf-8"
)
_app_handler.setLevel(LOG_LEVEL_FILE)
_app_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)-7s] %(module)s.%(funcName)s:%(lineno)d  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

_compress_handler = RotatingFileHandler(
    SCRIPT_LOG,
    maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COMPACT,
    encoding="utf-8"
)
_compress_handler.setLevel(LOG_LEVEL_COMPACT)
_compress_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

_stdout_handler = logging.StreamHandler()
_stdout_handler.setLevel(LOG_LEVEL_COMPACT)
_stdout_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))

_logger = logging.getLogger("video-manager")
_logger.setLevel(LOG_LEVEL_FILE)
_logger.addHandler(_app_handler)
_logger.addHandler(_compress_handler)
_logger.addHandler(_stdout_handler)
_logger.propagate = False

def log(msg, *args, level=logging.INFO):
    """兼容旧调用;新代码可直接用 _logger.info/.warning/.error/.debug。
    stacklevel=2 让 %(funcName)s/%(lineno)d 指向真正的 caller 而不是本 wrapper。"""
    _logger.log(level, msg, *args, stacklevel=2)

def json_response(handler, code, payload):
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)

def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[无法读取 {path}: {e}]"

# ============== 数据库 ==============
_db_lock = threading.Lock()
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn, _db_lock:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT NOT NULL,
            ended_at    TEXT,
            trigger     TEXT,           -- manual / cron / unknown
            success     INTEGER DEFAULT 0,
            skipped     INTEGER DEFAULT 0,
            failed      INTEGER DEFAULT 0,
            total       INTEGER DEFAULT 0,
            note        TEXT
        );
        CREATE TABLE IF NOT EXISTS run_files (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id    INTEGER,
            name      TEXT,
            ok        INTEGER,         -- 1 成功 0 失败 -1 跳过
            orig_size TEXT,
            new_size  TEXT,
            duration  INTEGER,         -- 秒
            started_at TEXT,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            rel_path     TEXT NOT NULL UNIQUE,            -- 相对 /input 路径
            size         INTEGER,
            status       TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed|skipped
            attempts     INTEGER DEFAULT 0,
            last_error   TEXT,
            last_run_id  INTEGER,
            created_at   TEXT DEFAULT (datetime('now','localtime')),
            started_at   TEXT,
            ended_at     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_rel    ON tasks(rel_path);
        -- 增量迁移:输出大小(只对新任务有值)
        -- SQLite 不支持 IF NOT EXISTS 列，用 PRAGMA 防御
        """)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "output_size" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN output_size INTEGER")
        # settings 表（路径等运行时可改配置）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # schedules 表（UI 配置的定时调度）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                cron_expr       TEXT NOT NULL,
                enabled         INTEGER NOT NULL DEFAULT 1,
                trigger_payload TEXT NOT NULL DEFAULT '{"trigger":"cron"}',
                last_run        TEXT,
                last_status     TEXT,
                created_at      TEXT DEFAULT (datetime('now','localtime')),
                updated_at      TEXT
            )
        """)
        # cluster.peers 默认值（多机集群模式下的 peer 列表）
        cur = conn.execute("SELECT value FROM settings WHERE key='cluster.peers'").fetchone()
        if not cur:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('cluster.peers','[]')"
            )
        # cluster.self.* 默认值（本机在集群里的身份）
        for k in ['cluster.self.id', 'cluster.self.name', 'cluster.self.url']:
            cur = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
            if not cur:
                conn.execute("INSERT INTO settings(key,value) VALUES(?, '')", (k,))
        # 崩溃恢复:把上轮未结束的 running 重置为 pending
        conn.execute(
            "UPDATE tasks SET status='pending', started_at=NULL "
            "WHERE status='running'"
        )
        conn.commit()

# ============== 进程管理 ==============
_state_lock   = threading.Lock()
_worker_thread = None              # Thread 对象(取代旧的 _proc = Popen)
_run_id       = None              # 当前 runs.id
_current_file = None              # 解析自 log 的 "正在处理" 文件
_started_at   = None

def proc_alive():
    global _worker_thread
    return _worker_thread is not None and _worker_thread.is_alive()

# 检测系统中是否有"看起来像压缩任务"的进程在跑
# 返回 dict 或 None
_ext_cache_ts = 0
_ext_cache    = None
def detect_external_job():
    """返回 {pid, script_pid, current_file, started_at} 或 None。"""
    global _ext_cache, _ext_cache_ts
    now = time.time()
    if _ext_cache and now - _ext_cache_ts < 1.5:
        return _ext_cache
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid,etime,cmd"],
            capture_output=True, text=True, timeout=3
        )
        script_pid = None
        started_at = None
        for line in r.stdout.splitlines():
            if "compress_video.sh" in line and "grep" not in line:
                # parse etime -> started_at
                m = re.match(r"\s*(\d+)\s+([\d::-]+)\s+(.*)", line)
                if m:
                    pid = int(m.group(1))
                    etime = m.group(2)
                    script_pid = pid
                    started_at = etime_to_start(etime)
                    break
        # 找匹配的 ffmpeg 进程(用我们脚本的特征 flag)
        ffmpeg_pid = None
        cur_file = None
        for line in r.stdout.splitlines():
            if "ffmpeg" in line and ("-vf" in line and ("rkmpp" in line or "vaapi" in line or "libx264" in line or "libx265" in line)):
                m = re.match(r"\s*(\d+)\s+([\d::-]+)\s+(.*)", line)
                if m:
                    ffmpeg_pid = int(m.group(1))
                    cmd = m.group(3)
                    mi = re.search(r"-i\s+(\S+)", cmd)
                    if mi: cur_file = os.path.basename(mi.group(1))
                    break
        if script_pid:
            _ext_cache = {
                "pid":          ffmpeg_pid or script_pid,
                "script_pid":   script_pid,
                "current_file": cur_file,
                "started_at":   started_at,
                "external":     True,
            }
        else:
            _ext_cache = None
        _ext_cache_ts = now
        return _ext_cache
    except Exception as e:
        log(f"detect_external_job 失败: {e}", level=logging.ERROR)
        return None

def etime_to_start(etime):
    """ps etime ('HH:MM:SS' 或 'MM:SS' 或 'D-HH:MM:SS') -> 起始时间字符串"""
    try:
        days = 0
        if "-" in etime:
            d, etime = etime.split("-", 1)
            days = int(d)
        parts = etime.split(":")
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(parts[1])
        else:
            return None
        delta = timedelta(days=days, hours=h, minutes=m, seconds=s)
        return (datetime.now() - delta).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def get_state():
    with _state_lock:
        if proc_alive():
            return {
                "running":      True,
                "pid":          os.getpid(),
                "run_id":       _run_id,
                "current_file": _current_file,
                "started_at":   _started_at,
                "external":     False,
            }
    # 没有自己启动的进程,检查外部
    ext = detect_external_job()
    if ext:
        return {
            "running":      True,
            "pid":          ext["pid"],
            "script_pid":   ext["script_pid"],
            "current_file": ext["current_file"],
            "started_at":   ext["started_at"],
            "external":     True,
            "run_id":       None,
        }
    return {
        "running":      False,
        "pid":          None,
        "run_id":       None,
        "current_file": None,
        "started_at":   None,
        "external":     False,
    }

# 在 log 文件上做"自上次读取以来的增量"跟踪(基于行号),用于实时显示
_log_pos_lock = threading.Lock()
_log_pos       = 0

def read_log_tail(limit=200, since=0, level="all", search=None, max_lines=5000):
    """读日志尾部。
    since>0: 只返回该行号之后的内容
    level:   all / error / warn / info / ok
    search:  关键字过滤(行内包含)
    max_lines: 服务器端最多返回这么多行(避免一次拉太多)
    """
    try:
        text = SCRIPT_LOG.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], 0
    lines = text.splitlines()
    total = len(lines)

    def match_level(line: str, lv: str) -> bool:
        if lv == "all":
            return True
        lc = line.lower()
        if lv == "error":
            return any(k in line for k in ("失败", "错误", "error", "exit=", "fatal", "Exception"))
        if lv == "warn":
            return any(k in line for k in ("警告", "warn", "超时"))
        if lv == "ok":
            return any(k in line for k in ("完成", "成功", "已启动", "已停止", "启动:"))
        if lv == "info":
            # "info" = 不属于 warn/error/ok 的其他行
            return not any(k in line for k in
                ("失败", "错误", "error", "exit=", "fatal", "Exception",
                 "警告", "warn", "超时",
                 "完成", "成功", "已启动", "已停止", "启动:"))
        return True

    def match_search(line: str, q: str) -> bool:
        if not q:
            return True
        return q.lower() in line.lower()

    # 过滤
    filtered = [
        (i + 1, ln) for i, ln in enumerate(lines)
        if match_level(ln, level) and match_search(ln, search or "")
    ]

    if since > 0:
        # since 是上次返回的最大行号
        filtered = [(n, ln) for n, ln in filtered if n > since]
    else:
        # 取最后 limit 行
        filtered = filtered[-limit:]

    # 限制最大返回
    if len(filtered) > max_lines:
        filtered = filtered[-max_lines:]

    # 返回 (line_no, text)
    return [(n, ln) for n, ln in filtered], total

# 解析日志,获取"当前正在处理的文件"(最近一条 [开始] 而其后无 [完成]/[失败]/[跳过])
_current_file_lock = threading.Lock()
def update_current_file():
    """从 log 解析当前正在压缩的文件,写入全局状态。"""
    global _current_file
    try:
        text = SCRIPT_LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    started = None
    finished_names = set()
    for line in text.splitlines()[-500:]:  # 只看最近 500 行
        m = re.search(r"\[开始\] (.+?)（", line)
        if m:
            started = m.group(1)
        m2 = re.search(r"\[完成\] (.+?) \|", line)
        if m2:
            finished_names.add(m2.group(1))
        m3 = re.search(r"\[失败\] (.+?) ", line)
        if m3:
            finished_names.add(m3.group(1))
        m4 = re.search(r"\[跳过\] (.+?)（", line)
        if m4:
            finished_names.add(m4.group(1))
    cur = None
    if started and started not in finished_names:
        cur = started
    with _current_file_lock, _state_lock:
        _current_file = cur

# ============== Worker (Python 取代 compress_video.sh) ==============
import fcntl as _fcntl

# 编码参数 (从 compress_video.sh 同步)
_OUTPUT_HEIGHT  = 720
_OUTPUT_FPS     = 10
_OUTPUT_WIDTH   = 1280  # 仅用于日志,实际靠 -vf scale=-2:HEIGHT
_SOFT_CODEC     = "libx264"
_SOFT_PRESET    = "veryfast"
_SOFT_CRF       = 28
_VAAPI_QP       = 28
_NICE_LEVEL     = 10
_MIN_FILE_SIZE  = 1_048_576  # 1MB,小于视为损坏
_MAX_LOG_LINES  = 2000

def _get_setting_int(key: str, default: int) -> int:
    try:
        with db() as conn:
            r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if r and r["value"]:
                return int(r["value"])
    except Exception:
        pass
    return default

def _get_setting_bool(key: str, default: bool) -> bool:
    try:
        with db() as conn:
            r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if r and r["value"]:
                return r["value"] in ("1", "true", "yes", "on")
    except Exception:
        pass
    return default

def _get_rkmpp_qp() -> int:
    """从 settings 读 RKMPP QP,默认 28。范围 18-36。"""
    v = _get_setting_int("rkmpp_qp", 28)
    return max(18, min(36, v))

def _get_rkmpp_bitrate_cap() -> int:
    """从 settings 读 RKMPP 上限码率(kbps),默认 4000。0=不限。"""
    v = _get_setting_int("rkmpp_bitrate_cap", 4000)
    return max(0, v)

def _get_force_recompress() -> bool:
    """UI 开关：on 时重新压缩所有 input(覆盖旧 output)。"""
    return _get_setting_bool("force_recompress", False)

_LOCK_FILE      = Path(os.environ.get("VIDEO_MANAGER_LOCK_FILE",
                                    str(APP_DIR / "compress.lock")))
_SCRIPT_LOG     = Path("/scripts/compress.log")

_worker_thread  = None      # Thread 对象
_ffmpeg_proc    = None      # 当前 ffmpeg 子进程(用于 stop)
_stop_event     = threading.Event()

def _rotate_log():
    """已废弃:_compress_handler (RotatingFileHandler) 自动按 size 轮转。
    保留为空函数以兼容 worker 末尾的旧调用点。"""
    pass

def _resolve_ffmpeg_bin() -> str:
    for path in [
        "/usr/local/bin/ffmpeg-rkmpp",
        "/usr/local/rkmpp/ffmpeg",
        "/ugreen/@appstore/com.ugreen.transcode/lib/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]:
        try:
            p = Path(path)
            if p.is_file() and os.access(path, os.X_OK):
                return path
        except OSError:
            continue
    return "/usr/bin/ffmpeg"

def _probe_encoders(ffmpeg_bin: str) -> str:
    try:
        r = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout
    except Exception as e:
        log(f"枚举编码器失败: {e}", level=logging.ERROR)
        return ""

def _probe_hwaccel_ok(ffmpeg_bin: str, mode: str) -> bool:
    """运行一个微编码测试,退出码为 0 表示硬件可用。"""
    if mode == "rkmpp":
        cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1:r=10",
               "-c:v", "h264_rkmpp", "-qp", "28", "-rc_mode", "2",
               "-frames:v", "1", "-f", "null", "-"]
    elif mode == "vaapi":
        cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-init_hw_device", "vaapi=foo:/dev/dri/renderD128",
               "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1:r=10",
               "-frames:v", "1", "-f", "null", "-"]
    else:
        return False
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False

def _detect_hwaccel() -> str:
    """镜像 compress_video.sh 的 detect_hwaccel 逻辑。"""
    ffmpeg_bin = _resolve_ffmpeg_bin()
    encs = _probe_encoders(ffmpeg_bin)

    # 0. RKMPP
    if Path("/dev/mpp_service").exists():
        if "hevc_rkmpp" in encs:
            if _probe_hwaccel_ok(ffmpeg_bin, "rkmpp"):
                return "rkmpp"
            log("MPP 设备在但 h264_rkmpp 探测失败", level=logging.WARNING)
        else:
            log("FFMPEG_BIN 缺少 hevc_rkmpp 编码器,可能用错二进制", level=logging.WARNING)

    # 1. 没 GPU 设备就软
    if not Path("/dev/dri/renderD128").exists() and not Path("/dev/dri/card0").exists():
        return "soft"

    # 2. VAAPI
    if "hevc_vaapi" in encs:
        if _probe_hwaccel_ok(ffmpeg_bin, "vaapi"):
            return "vaapi"
        log("VAAPI 设备存在但初始化失败", level=logging.WARNING)

    # 3. V4L2
    if "hevc_v4l2m2m" in encs:
        try:
            r = subprocess.run("ls /dev/video* 2>/dev/null | head -1",
                               shell=True, capture_output=True, text=True, timeout=3)
            dev = r.stdout.strip()
            if dev:
                pr = subprocess.run(
                    [ffmpeg_bin, "-hide_banner", "-f", "v4l2",
                     "-list_formats", "all", "-i", dev],
                    capture_output=True, text=True, timeout=10,
                )
                if "HEVC" in (pr.stdout + pr.stderr):
                    return "v4l2m2m"
        except Exception as e:
            log(f"v4l2 探测异常: {e}", level=logging.WARNING)

    # 4. QSV
    if "hevc_qsv" in encs:
        return "qsv"

    return "soft"

def _build_ffmpeg_cmd(input_file: Path, output_file: Path, hwaccel: str, ffmpeg_bin: str) -> list:
    base = ["nice", "-n", str(_NICE_LEVEL),
            ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-err_detect", "ignore_err", "-fflags", "+discardcorrupt"]
    if hwaccel == "rkmpp":
        # RKMPP 优化管线:硬件解码(drm_prime 零拷贝) → RGA 硬缩 → MPP 硬编
        # 实测 RK3588: 12x+ 实时（软缩放只能 2x）
        # 只设 vpp_rkrga 缩放,framerate 不强制(source 通常 20fps,压缩比够好)
        # 加 format/fps 会造成 auto_scale_0 格式不兼容
        # 码率控制:CQP(变码率,跟着内容复杂度走)
        #   - 静态场景(监控大多数时候)自动低码率,文件变小
        #   - 动态场景(有人动)保持质量
        #   - -b:v 作为上限防意外(动态场景不会跳到 100MB)
        #   - QP 和 cap 都可以在 配置 页调
        qp = _get_rkmpp_qp()
        cap = _get_rkmpp_bitrate_cap()
        cmd = [
            "-hwaccel", "rkmpp",
            "-hwaccel_output_format", "drm_prime",
            "-i", str(input_file),
            "-vf", f"vpp_rkrga=w=-2:h={_OUTPUT_HEIGHT}",
            "-c:v", "h264_rkmpp",
            "-qp", str(qp), "-rc_mode", "2",  # CQP 变码率
        ]
        if cap > 0:
            cmd += ["-b:v", f"{cap}k"]
        cmd += ["-an", "-movflags", "+faststart", "-y", str(output_file)]
        return base + cmd
    if hwaccel == "vaapi":
        return base + [
            "-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128",
            "-vaapi_device", "/dev/dri/renderD128",
            "-i", str(input_file),
            "-vf", f"format=nv12|vaapi,hwupload,scale_vaapi=-2:{_OUTPUT_HEIGHT}:format=nv12,framerate=fps={_OUTPUT_FPS}",
            "-c:v", "hevc_vaapi", "-qp", str(_VAAPI_QP),
            "-an", "-movflags", "+faststart", "-y", str(output_file),
        ]
    if hwaccel == "v4l2m2m":
        return base + [
            "-i", str(input_file),
            "-vf", f"scale=-2:{_OUTPUT_HEIGHT},fps={_OUTPUT_FPS}",
            "-c:v", "hevc_v4l2m2m", "-num_capture_buffers", "32",
            "-b:v", "1M", "-maxrate", "1.5M", "-bufsize", "2M",
            "-an", "-movflags", "+faststart", "-y", str(output_file),
        ]
    if hwaccel == "qsv":
        return base + [
            "-hwaccel", "qsv", "-c:v", "h264_qsv",
            "-i", str(input_file),
            "-vf", f"scale_qsv=-2:{_OUTPUT_HEIGHT},vpp_qsv=framerate={_OUTPUT_FPS}",
            "-c:v", "hevc_qsv", "-global_quality", str(_VAAPI_QP), "-preset", "medium",
            "-an", "-movflags", "+faststart", "-y", str(output_file),
        ]
    # soft
    return base + [
        "-threads", "0",
        "-i", str(input_file),
        "-vf", f"scale=-2:{_OUTPUT_HEIGHT},fps={_OUTPUT_FPS}",
        "-c:v", _SOFT_CODEC, "-crf", str(_SOFT_CRF), "-preset", _SOFT_PRESET,
        "-tune", "fastdecode",
        "-an", "-movflags", "+faststart", "-y", str(output_file),
    ]

def _run_ffmpeg(input_file: Path, output_file: Path, hwaccel: str) -> tuple:
    """跑一个文件,返回 (exit_code, stderr_text)。"""
    ffmpeg_bin = _resolve_ffmpeg_bin()
    cmd = _build_ffmpeg_cmd(input_file, output_file, hwaccel, ffmpeg_bin)
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        with _state_lock:
            _ffmpeg_proc = proc
        try:
            _, err = proc.communicate(timeout=4 * 3600)
            return proc.returncode, (err or b"").decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
            return -1, "ffmpeg 单文件超时(>4h)"
    except Exception as e:
        return -1, f"ffmpeg 启动失败: {e}"
    finally:
        with _state_lock:
            _ffmpeg_proc = None

def _set_task_status(task_id: int, status: str, **extra):
    """便捷更新 tasks 表。"""
    sets = ["status=?"]
    vals = [status]
    for k, v in extra.items():
        if v is None: continue
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(task_id)
    with db() as conn, _db_lock:
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()

def _run_loop(run_id: int, trigger: str):
    """Worker 主循环。跑在独立线程里。"""
    global _current_file
    log(f"========================================")
    log(f"压缩任务启动 (Python worker v1, run_id={run_id}, trigger={trigger})")
    ffmpeg_bin = _resolve_ffmpeg_bin()
    hwaccel = _detect_hwaccel()
    log(f"输入: {INPUT_DIR}  输出: {OUTPUT_DIR}")
    log(f"ffmpeg: {ffmpeg_bin}  加速: {hwaccel}  分辨率: {_OUTPUT_WIDTH}x{_OUTPUT_HEIGHT}@{_OUTPUT_FPS}fps")
    log(f"========================================")

    # flock
    lock_fd = None
    try:
        lock_fd = open(_LOCK_FILE, "w")
        _fcntl.flock(lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except (IOError, OSError):
        log("已有压缩任务运行中(flock 被占用),本实例退出")
        if lock_fd: lock_fd.close()
        return

    success = skipped = failed = 0
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        # 先把 /input /output 同步进 tasks
        try:
            sync_tasks_from_input()
        except Exception as e:
            log(f"sync_tasks 失败(继续): {e}")

        # 崩溃恢复:上轮没正常结束的 running 状态 重置为 pending(避免丢文件)
        try:
            with db() as conn, _db_lock:
                cur = conn.execute(
                    "UPDATE tasks SET status='pending', started_at=NULL "
                    "WHERE status='running'"
                )
                if cur.rowcount:
                    log(f"重置 {cur.rowcount} 个遗留 running → pending")
        except Exception as e:
            log(f"running 重置失败(继续): {e}")

        with db() as conn, _db_lock:
            pending = conn.execute("""
                SELECT id, rel_path, size FROM tasks
                WHERE status='pending' ORDER BY rel_path ASC
            """).fetchall()
        log(f"待处理: {len(pending)} 个 (按文件名时间正序)")

        for i, row in enumerate(pending, 1):
            if _stop_event.is_set():
                log(f"收到停止信号,提前结束(已处理 {i-1}/{len(pending)})")
                break

            tid, rel, size = row["id"], row["rel_path"], row["size"]
            input_file  = INPUT_DIR / rel
            output_file = OUTPUT_DIR / rel

            _set_task_status(tid, "running",
                             started_at=now_str(),
                             attempts=None,  # 用 SQL 自增在下面处理
                             last_run_id=run_id)
            # attempts 自增
            with db() as conn, _db_lock:
                conn.execute("UPDATE tasks SET attempts = attempts + 1 WHERE id=?", (tid,))
                conn.commit()

            with _state_lock:
                _current_file = rel

            # 输出已存在 -> 跳过(除非用户开了 force_recompress 重压开关)
            if output_file.exists() and not _get_force_recompress():
                _set_task_status(tid, "skipped", ended_at=now_str())
                skipped += 1
                log(f"[{i}/{len(pending)}] 跳过: {rel} (输出已存在)")
                continue
            if output_file.exists() and _get_force_recompress():
                # 强制重压:删旧 output
                try:
                    output_file.unlink()
                    log(f"[{i}/{len(pending)}] 强制重压: 删旧 {rel}")
                except OSError as e:
                    log(f"  警告: 删旧 output 失败: {e}", level=logging.WARNING)

            # 输入不在 -> 失败
            if not input_file.exists():
                _set_task_status(tid, "failed",
                                 last_error="input file not found",
                                 ended_at=now_str())
                failed += 1
                log(f"[{i}/{len(pending)}] 失败: {rel} (输入不存在)")
                continue

            output_file.parent.mkdir(parents=True, exist_ok=True)
            log(f"[{i}/{len(pending)}] 开始: {rel} ({human_size(size)})")
            start_ts = time.time()
            exit_code, err = _run_ffmpeg(input_file, output_file, hwaccel)
            duration = int(time.time() - start_ts)

            if exit_code == 0 and output_file.exists():
                out_size = output_file.stat().st_size
                if out_size > _MIN_FILE_SIZE:
                    # 成功:删输入,标 done + 记输出大小
                    try:
                        input_file.unlink()
                    except OSError as e:
                        log(f"  警告: 删除输入失败: {e}", level=logging.WARNING)
                    _set_task_status(tid, "done",
                                     ended_at=now_str(),
                                     output_size=out_size)
                    success += 1
                    log(f"  完成 -> {human_size(out_size)} ({duration}s)")
                else:
                    try: output_file.unlink()
                    except OSError: pass
                    _set_task_status(tid, "failed",
                                     last_error=f"output too small ({out_size}B)",
                                     ended_at=now_str())
                    failed += 1
                    log(f"  失败: 输出过小 {out_size}B")
            else:
                try: output_file.unlink()
                except OSError: pass
                err_msg = (err or f"exit={exit_code}")[:500]
                _set_task_status(tid, "failed",
                                 last_error=err_msg,
                                 ended_at=now_str())
                failed += 1
                log(f"  失败: exit={exit_code} {err_msg[:120]}")

        log(f"任务完成 | 成功:{success} 跳过:{skipped} 失败:{failed} 总:{len(pending)}")

        with db() as conn, _db_lock:
            conn.execute("""UPDATE runs SET ended_at=?, 
                success=?, skipped=?, failed=?, total=? WHERE id=?""",
                (now_str(), success, skipped, failed, len(pending), run_id))
            conn.commit()
        _rotate_log()
    finally:
        try:
            _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass
        with _state_lock:
            _current_file = None
        log(f"worker 退出")

def start_run(trigger="manual"):
    global _worker_thread, _run_id, _started_at, _current_file, _stop_event
    if proc_alive():
        return False, "本服务启动的任务正在运行"
    ext = detect_external_job()
    if ext:
        return False, f"检测到外部已有压缩任务在运行(script_pid={ext['script_pid']}),请先停止或等其完成"
    invalidate_ext_cache()
    try:
        _started_at = now_str()
        _current_file = None
        _stop_event.clear()
        # 先写 runs 表拿 run_id
        with db() as conn, _db_lock:
            cur = conn.execute(
                "INSERT INTO runs(started_at, trigger) VALUES(?, ?)",
                (_started_at, trigger)
            )
            _run_id = cur.lastrowid
        _worker_thread = threading.Thread(
            target=_run_loop, args=(_run_id, trigger), daemon=True
        )
        _worker_thread.start()
        log(f"启动任务 thread run_id={_run_id} trigger={trigger}")
        return True, f"已启动 run_id={_run_id}"
    except Exception as e:
        _worker_thread = None
        return False, f"启动失败: {e}"

def _watch_run(run_id):
    """适配旧名:不再使用。新逻辑都在 _run_loop 里。"""
    pass

def invalidate_ext_cache():
    global _ext_cache, _ext_cache_ts
    _ext_cache = None
    _ext_cache_ts = 0

def get_descendants(root_pid):
    """递归获取 root_pid 的所有子孙进程(含 root_pid 本身)。用于安全杀死进程树。"""
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid,ppid"],
            capture_output=True, text=True, timeout=3
        )
        children = {}
        for line in r.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    p = int(parts[0]); pp = int(parts[1])
                    children.setdefault(pp, []).append(p)
                except ValueError:
                    continue
        result, stack = [], [root_pid]
        while stack:
            cur = stack.pop()
            result.append(cur)
            stack.extend(children.get(cur, []))
        return result
    except Exception:
        return [root_pid]

def _kill_tree(pids, sig):
    for p in pids:
        try:
            os.kill(p, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            log(f"无权限杀 pid={p}", level=logging.WARNING)

def stop_run():
    """优先停自己启动的进程;否则停外部检测到的进程树(只杀脚本及其子进程,不碰用户 shell)。"""
    global _stop_event, _ffmpeg_proc
    invalidate_ext_cache()
    if proc_alive():
        # 告诉 worker 停止(下一个文件之前检查 stop_event)
        _stop_event.set()
        # 同时 SIGTERM 当前 ffmpeg 子进程(如果有),2 秒后 SIGKILL
        proc = None
        with _state_lock:
            proc = _ffmpeg_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
        log("已发送停止信号")
        # 等 worker 退出(最多 10s)
        try:
            if _worker_thread:
                _worker_thread.join(timeout=10)
        except Exception:
            pass
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGKILL)
            except ProcessLookupError:
                pass
        with _state_lock:
            _worker_thread = None
            _ffmpeg_proc   = None
        return True, "已发送停止信号,worker 退出"
    ext = detect_external_job()
    if ext:
        # 外部任务:只杀脚本及其子孙,不碰用户终端 session
        script_pid = ext["script_pid"]
        pids = get_descendants(script_pid)
        log(f"外部任务进程树: {pids}")
        _kill_tree(pids, signal.SIGTERM)
        time.sleep(2)
        # SIGKILL 残留
        survivors = [p for p in pids if _pid_alive(p)]
        _kill_tree(survivors, signal.SIGKILL)
        time.sleep(0.5)
        invalidate_ext_cache()
        log(f"已停止外部任务 script_pid={script_pid}, 进程 {len(pids)} 个")
        return True, f"已停止外部任务 (script_pid={script_pid}, 进程 {len(pids)} 个)"
    return False, "当前没有任务在运行"

def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False

# ============== 脚本配置管理 ==============
# 在 compress_video.sh 里,我们只允许改 "配置区" 的几个变量
CONFIG_KEYS = [
    "OUTPUT_WIDTH", "OUTPUT_HEIGHT", "OUTPUT_FPS",
    "SOFT_CODEC", "SOFT_PRESET", "SOFT_CRF",
    "VAAPI_QP", "NICE_LEVEL", "MAX_LOG_LINES", "MIN_FILE_SIZE",
]
_config_lock = threading.Lock()
_config_cache = {"text": None, "mtime": 0}

def read_script_config():
    """解析配置区变量。"""
    with _config_lock:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        result = {}
        for line in text.splitlines():
            line_strip = line.strip()
            for k in CONFIG_KEYS:
                if line_strip.startswith(k + "="):
                    raw = line_strip.split("=", 1)[1]
                    # 去掉行内注释
                    raw = raw.split("#", 1)[0].strip()
                    # 去引号
                    if (raw.startswith('"') and raw.endswith('"')) or \
                       (raw.startswith("'") and raw.endswith("'")):
                        raw = raw[1:-1]
                    result[k] = raw
                    break
        return result, text

def update_script_config(updates: dict):
    """替换脚本里对应的变量赋值,保留其他内容。"""
    with _config_lock:
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        lines = text.splitlines()
        out = []
        for line in lines:
            replaced = False
            for k, v in updates.items():
                if k in CONFIG_KEYS and re.match(rf"^\s*{k}\s*=", line):
                    if isinstance(v, str) and re.search(r"\s", v):
                        new_line = re.sub(rf"^(\s*){k}\s*=.*$", rf'\1{k}="{v}"', line)
                    else:
                        new_line = re.sub(rf"^(\s*){k}\s*=.*$", rf"\1{k}={v}", line)
                    out.append(new_line)
                    replaced = True
                    break
            if not replaced:
                out.append(line)
        new_text = "\n".join(out) + ("\n" if text.endswith("\n") else "")
        # 备份
        bak = SCRIPT_PATH.with_suffix(".sh.bak.manager")
        bak.write_text(text, encoding="utf-8")
        SCRIPT_PATH.write_text(new_text, encoding="utf-8")
        log(f"脚本配置已更新,备份到 {bak}")
        return True

# ============== Ofelia 配置管理 ==============
_ofelia_lock = threading.Lock()
def read_ofelia_jobs():
    """用 configparser 解析 ofelia.ini 中的 [job-exec "xxx"] 段。"""
    if not OFELIA_INI.exists():
        return []
    cfg = configparser.ConfigParser()
    # 保留大小写
    cfg.optionxform = str
    try:
        cfg.read(OFELIA_INI, encoding="utf-8")
    except Exception as e:
        log(f"读 ofelia.ini 失败: {e}", level=logging.ERROR)
        return []
    jobs = []
    for section in cfg.sections():
        if section.startswith("job-exec") or section.startswith("job-run") or section.startswith("job-local"):
            sec = cfg[section]
            # 从段标题解析名字,例:job-exec "compress-surveillance" -> compress-surveillance
            m = re.match(r'^job-\w+\s+["\']([^"\']+)["\']', section)
            derived_name = m.group(1) if m else section
            jobs.append({
                "section":   section,
                "name":      sec.get("name") or derived_name,
                "schedule":  sec.get("schedule", ""),
                "container": sec.get("container", ""),
                "command":   sec.get("command", ""),
            })
    return jobs

def update_ofelia_jobs(jobs: list):
    """重写 ofelia.ini 中的所有 job-exec 段,保留其他内容(注释等)。"""
    with _ofelia_lock:
        # 备份
        if OFELIA_INI.exists():
            OFELIA_BAK.write_text(OFELIA_INI.read_text(encoding="utf-8"), encoding="utf-8")
        # 重建:把文件按段拆分,只替换 [job-exec ...] 段
        text = OFELIA_INI.read_text(encoding="utf-8") if OFELIA_INI.exists() else ""
        # 简单处理:行级扫描,把 job-exec 段替换成新内容
        lines = text.splitlines()
        out = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if stripped.startswith("[job-") and stripped.endswith("]"):
                # 跳到段尾
                i += 1
                while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith("["):
                    i += 1
                continue
            out.append(line)
            i += 1
        # 删除所有原 job- 段后的空行(连续空行合并)
        # 简化:直接重写文件,只保留头部注释
        header_lines = []
        for ln in out:
            if ln.strip().startswith("["):
                break
            header_lines.append(ln)
        # 去除尾部空行
        while header_lines and not header_lines[-1].strip():
            header_lines.pop()

        new_text = "\n".join(header_lines) + "\n\n"
        for job in jobs:
            sec_name = job.get("section") or f'job-exec "{job.get("name","job")}"'
            new_text += f"[{sec_name}]\n"
            if job.get("name"):
                new_text += f"name      = {job['name']}\n"
            new_text += f"schedule  = {job.get('schedule','')}\n"
            new_text += f"container = {job.get('container','')}\n"
            new_text += f"command   = {job.get('command','')}\n\n"

        OFELIA_INI.write_text(new_text, encoding="utf-8")
        log(f"ofelia.ini 已重写,任务数: {len(jobs)}")
        return True

def restart_ofelia():
    """通过 sudo 免密重启 ofelia 容器。需要 sudoers 配置 NOPASSWD。"""
    cmd = ["sudo", "-n", "docker", "restart", "ofelia-scheduler"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return True, f"ofelia 已重启: {r.stdout.strip() or 'OK'}"
        msg = (r.stderr or r.stdout or "").strip() or f"退出码 {r.returncode}"
        if r.returncode != 0 and "password" in msg.lower():
            msg += "\n提示: 需 sudoers 配置免密: kxrdyf ALL=(root) NOPASSWD: /usr/bin/docker restart ofelia-scheduler"
        return False, f"重启失败: {msg}"
    except FileNotFoundError:
        return False, "未找到 sudo 或 docker，请手动执行: docker restart ofelia-scheduler"
    except Exception as e:
        return False, f"重启失败: {e}"

# ============== Cron 表达式:计算下一次运行时间 ==============
def cron_next_run(expr: str, base: datetime = None) -> str:
    """简易 cron 计算:支持标准 5 字段(m h dom mon dow)。返回 'YYYY-MM-DD HH:MM:SS' 或 'invalid'。"""
    if not expr or not expr.strip():
        return ""
    parts = expr.split()
    if len(parts) != 5:
        return "invalid"
    minute, hour, dom, month, dow = parts
    base = base or datetime.now()

    def parse_field(field, lo, hi):
        field = field.strip()
        # 支持 * / , -
        if field == "*":
            return set(range(lo, hi + 1))
        vals = set()
        for part in field.split(","):
            step = 1
            if "/" in part:
                part, s = part.split("/", 1)
                step = int(s)
            if part == "*":
                rng = range(lo, hi + 1, step)
            elif "-" in part:
                a, b = part.split("-", 1)
                rng = range(int(a), int(b) + 1, step)
            else:
                start = int(part)
                end = hi if step > 1 else start
                rng = range(start, end + 1, step)
            for v in rng:
                if lo <= v <= hi:
                    vals.add(v)
        return vals

    try:
        mins   = parse_field(minute, 0, 59)
        hours  = parse_field(hour,   0, 23)
        doms   = parse_field(dom,    1, 31)
        months = parse_field(month,  1, 12)
        dows   = parse_field(dow,    0, 6)
    except Exception:
        return "invalid"

    # cron: dow 0=Sun;Python: weekday() 0=Mon... 转
    py_dows = set((d + 6) % 7 for d in dows)  # cron 0->py 6
    # 搜索未来 366 天
    cur = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if cur.month in months and cur.day in doms and cur.weekday() in py_dows and cur.hour in hours and cur.minute in mins:
            return cur.strftime("%Y-%m-%d %H:%M:%S")
        cur += timedelta(minutes=1)
    return "invalid"

# ============== 系统信息 ==============
def detect_hwaccel_hint():
    """复用脚本里的探测逻辑(简化版),给前端一个 hint。"""
    hints = []
    if os.path.exists("/dev/mpp_service"):
        hints.append("rkmpp 设备存在")
    if os.path.exists("/dev/dri/renderD128"):
        hints.append("VAAPI 可用")
    for cand in ["/usr/local/bin/ffmpeg-rkmpp", "/usr/local/rkmpp/ffmpeg",
                 "/ugreen/@appstore/com.ugreen.transcode/lib/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            hints.append(f"ffmpeg: {cand}")
            return hints
    hints.append("ffmpeg 未找到")
    return hints

def ffmpeg_version():
    for cand in ["/usr/local/bin/ffmpeg-rkmpp", "/usr/local/rkmpp/ffmpeg",
                 "/ugreen/@appstore/com.ugreen.transcode/lib/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            try:
                r = subprocess.run([cand, "-version"], capture_output=True, text=True, timeout=5)
                return cand, r.stdout.splitlines()[0] if r.stdout else "(empty)"
            except Exception:
                return cand, "(运行失败)"
    return None, "未找到 ffmpeg"

# ============== HTTP 路由 ==============
class Handler(BaseHTTPRequestHandler):
    # 升级到 HTTP/1.1:浏览器视频流(尤其是 Range seek)需要 keep-alive + chunked
    protocol_version = "HTTP/1.1"
    # 禁掉默认 keep-alive 的 5s 超时(socket 默认),改成系统级
    # (ThreadingHTTPServer 会处理)
    def log_message(self, fmt, *args):
        # 静默访问日志(我们自己 log)
        pass

    def _safe_join(self, base: Path, rel: str):
        """防止路径穿越。base 必须存在。"""
        if not rel:
            return None
        # 拒绝绝对路径、反斜杠、空字节
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

    def _stream_file(self, dir_param: str, file_param: str):
        """视频流,支持 Range 请求 + CORS。"""
        if dir_param == "input":
            base = INPUT_DIR
        elif dir_param == "output":
            base = OUTPUT_DIR
        else:
            return self.send_error(400, "dir must be 'input' or 'output'")
        fp = self._safe_join(base, file_param)
        if not fp or not fp.is_file():
            return self.send_error(404, "not found")
        if not os.access(fp, os.R_OK):
            return self.send_error(403, "not readable")
        file_size = fp.stat().st_size
        # 猜 mime
        suffix = fp.suffix.lower()
        mime = {
            ".mp4": "video/mp4", ".m4v": "video/mp4",
            ".webm": "video/webm", ".mkv": "video/x-matroska",
            ".mov": "video/quicktime", ".avi": "video/x-msvideo",
        }.get(suffix, "application/octet-stream")
        range_header = self.headers.get("Range")
        start, end, length = 0, file_size - 1, file_size
        status = 200
        if range_header:
            import re as _re
            m = _re.match(r'^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$', range_header)
            if not m:
                self.send_header("Content-Range", f"bytes */{file_size}")
                return self.send_error(416, "invalid Range")
            s_str, e_str = m.group(1), m.group(2)
            if s_str == "" and e_str != "":
                # bytes=-N: 最后 N 字节
                length = int(e_str)
                start = max(0, file_size - length)
                end = file_size - 1
            else:
                start = int(s_str) if s_str else 0
                end = int(e_str) if e_str else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                self.send_header("Content-Range", f"bytes */{file_size}")
                return self.send_error(416, "Range out of bounds")
            length = end - start + 1
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Accept-Ranges, Content-Length")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        # 流式读,8K chunk
        try:
            with open(fp, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk_size = min(8192, remaining)
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端拖走/暂停/关闭,正常
            pass
        except Exception as e:
            log(f"stream error {fp}: {e}", level=logging.WARNING)
        return None

    def _download_file(self, dir_param: str, file_param: str):
        """文件下载,Content-Disposition: attachment。"""
        if dir_param == "input":
            base = INPUT_DIR
        elif dir_param == "output":
            base = OUTPUT_DIR
        else:
            return self.send_error(400, "dir must be 'input' or 'output'")
        fp = self._safe_join(base, file_param)
        if not fp or not fp.is_file():
            return self.send_error(404, "not found")
        file_size = fp.stat().st_size
        name = fp.name
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(file_size))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{_urlquote(name)}")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            with open(fp, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk: break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    def _proxy_peer_stream(self, qs, as_attachment: bool):
        """从配置的远端 peer 拉文件,转给客户端。处理 HTTPS Mixed Content。
        qs: ?peer=ID&dir=input|output&path=xxx
        as_attachment: True=下载,False=流播放
        """
        peer_id = qs.get("peer", [""])[0]
        dir_param = qs.get("dir", ["output"])[0]
        file_param = qs.get("path", [""])[0]
        if not peer_id or not file_param:
            return json_response(self, 400, {"ok": False, "error": "需要 peer 和 path 参数"})
        # 查 peer URL
        try:
            raw = _get_setting("cluster.peers", "[]")
            peers = json.loads(raw) if raw else []
        except Exception:
            peers = []
        peer = next((p for p in peers if p.get("id") == peer_id), None)
        if not peer:
            return json_response(self, 404, {"ok": False, "error": f"peer {peer_id!r} 不存在"})
        target_url = f"{peer['url'].rstrip('/')}/api/files/stream?dir={dir_param}&path={_urlquote(file_param)}"
        # 转发 Range
        range_header = self.headers.get("Range")
        headers = {"User-Agent": "video-manager-proxy/1.0"}
        if range_header:
            headers["Range"] = range_header
        req = _urlreq.Request(target_url, headers=headers)
        try:
            upstream = _urlreq.urlopen(req, timeout=30)
        except Exception as e:
            return json_response(self, 502, {"ok": False, "error": f"上游 peer 不可达: {e}"})
        # 透传上游响应头 + 状态码(200/206)
        status = upstream.status
        # 透传关键头
        passthrough = {
            "Content-Type", "Content-Length", "Content-Range",
            "Accept-Ranges", "Cache-Control",
        }
        self.send_response(status)
        for h, v in upstream.getheaders():
            if h in passthrough:
                self.send_header(h, v)
        # 覆盖 CORS / Connection
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Accept-Ranges, Content-Length")
        self.send_header("Connection", "keep-alive")
        if as_attachment:
            name = Path(file_param).name
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{_urlquote(name)}")
        self.end_headers()
        # 流式透传 body
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"proxy stream error: {e}", level=logging.WARNING)
        return None

    def _serve_thumbnail(self, dir_param: str, file_param: str):
        """缩略图接口。返回 jpeg,浏览器可缓存 1 天。"""
        thumb = get_or_make_thumbnail(dir_param, file_param)
        if not thumb:
            # 返回 1x1 透明 png(避免浏览器被取一次抦1x1打破缓存设计)
            placeholder = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
                b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(placeholder)))
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(placeholder)
            return None
        try:
            data = thumb.read_bytes()
        except Exception as e:
            log(f"thumb read error: {e}", level=logging.WARNING)
            return self.send_error(500, str(e))
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    def _serve_pb_thumb(self, qs):
        """播放进度条单帧缩略图。返回 jpeg,可缓存 1 天。"""
        dir_param = qs.get("dir", ["output"])[0]
        h = qs.get("h", [""])[0]
        i = qs.get("i", ["0"])[0]
        # 安全: h 是 md5 hex 前 16 位, i 是整数
        import re as _re
        if not _re.match(r'^[a-f0-9]{1,32}$', h) or not _re.match(r'^\d{1,3}$', i):
            return self.send_error(400, "bad params")
        thumb_path = PB_THUMB_DIR / dir_param / f"{h}_{i}.jpg"
        if not thumb_path.exists() or not thumb_path.is_file():
            return self.send_error(404, "thumb not found")
        try:
            data = thumb_path.read_bytes()
        except Exception as e:
            log(f"pb-thumb read error: {e}", level=logging.WARNING)
            return self.send_error(500, str(e))
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    def do_OPTIONS(self):
        # 处理 CORS preflight
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()
        return None

    def _read_json_body(self):
        """PATCH/DELETE/POST 共用的 body 解析"""
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def do_PATCH(self):
        """路由 /api/cluster/peers/<id> — 以 PATCH 改 url/name"""
        url = urlparse(self.path)
        path = url.path
        m = re.match(r"^/api/cluster/peers/([A-Za-z0-9_-]{1,32})/?$", path)
        if not m:
            return self.send_error(404)
        data = self._read_json_body()
        return self._handle_peer_op(m.group(1), data)

    def do_DELETE(self):
        """路由 /api/cluster/peers/<id> — 删除节点"""
        url = urlparse(self.path)
        path = url.path
        m = re.match(r"^/api/cluster/peers/([A-Za-z0-9_-]{1,32})/?$", path)
        if not m:
            return self.send_error(404)
        return self._handle_peer_op(m.group(1), {})

    def _serve_file(self, path, ctype="text/plain"):
        try:
            data = Path(path).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _handle_peer_op(self, peer_id: str, data: dict):
        """单节点 POST/PATCH/DELETE 路由 /api/cluster/peers/<id>
        install.sh 在 worker 节点上 join 时调 POST
        UI 在管理面板上 add/remove/toggle 时调 POST/PATCH/DELETE
        """
        method = self.command  # POST / PATCH / DELETE
        try:
            raw = _get_setting("cluster.peers", "[]")
            peers = json.loads(raw) if raw else []
        except Exception:
            peers = []

        if method == "POST":
            url = (data.get("url") or "").strip().rstrip("/")
            name = (data.get("name") or "").strip() or peer_id
            if not url.startswith(("http://", "https://")):
                return json_response(self, 400, {"ok": False, "error": "url 必须以 http:// 或 https:// 开头"})
            if any(p.get("id") == peer_id for p in peers):
                return json_response(self, 409, {"ok": False, "error": f"节点 {peer_id} 已存在"})
            peers.append({"id": peer_id, "name": name, "url": url})
            update_peers(peers)
            log(f"集群: 添加节点 {peer_id} -> {url}")
            return json_response(self, 200, {"ok": True, "id": peer_id, "url": url})

        if method == "DELETE":
            new_peers = [p for p in peers if p.get("id") != peer_id]
            if len(new_peers) == len(peers):
                return json_response(self, 404, {"ok": False, "error": f"节点 {peer_id} 不存在"})
            update_peers(new_peers)
            log(f"集群: 移除节点 {peer_id}")
            return json_response(self, 200, {"ok": True})

        if method == "PATCH":
            target = next((p for p in peers if p.get("id") == peer_id), None)
            if not target:
                return json_response(self, 404, {"ok": False, "error": f"节点 {peer_id} 不存在"})
            if "url" in data:
                new_url = (data.get("url") or "").strip().rstrip("/")
                if not new_url.startswith(("http://", "https://")):
                    return json_response(self, 400, {"ok": False, "error": "url 必须以 http:// 或 https:// 开头"})
                target["url"] = new_url
            if "name" in data:
                target["name"] = (data.get("name") or peer_id).strip()
            update_peers(peers)
            return json_response(self, 200, {"ok": True})

        return json_response(self, 405, {"ok": False, "error": f"不支持的方法 {method}"})

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)

        # 静态
        if path == "/" or path == "/index.html":
            return self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            full = STATIC_DIR / rel
            if not str(full.resolve()).startswith(str(STATIC_DIR.resolve())):
                return self.send_error(403)
            ctype = "application/javascript" if rel.endswith(".js") else \
                    "text/css" if rel.endswith(".css") else \
                    "image/png" if rel.endswith(".png") else \
                    "image/svg+xml" if rel.endswith(".svg") else \
                    "text/plain"
            return self._serve_file(full, ctype)

        # API
        if path == "/api/status":
            st = get_state()
            st["log_lines"] = sum(1 for _ in open(SCRIPT_LOG, encoding="utf-8", errors="replace")) if SCRIPT_LOG.exists() else 0
            st["lock_exists"] = SCRIPT_LOCK.exists()
            # 加一个提示信息
            if st["running"] and st.get("external"):
                st["hint"] = "检测到外部任务在运行(从终端启动),可在「任务」页停止"
            elif st["running"]:
                st["hint"] = "任务运行中"
            else:
                st["hint"] = "空闲,可以从「任务」页启动"
            return json_response(self, 200, st)

        if path == "/api/current-file":
            update_current_file()
            return json_response(self, 200, {"current_file": get_state()["current_file"]})

        if path == "/api/logs":
            since  = int(qs.get("since",  ["0"])[0])
            limit  = int(qs.get("limit",  ["500"])[0])
            level  = (qs.get("level", ["all"])[0] or "all")
            search = (qs.get("q",     [None])[0] or None)
            try:
                max_lines = int(qs.get("max_lines", ["5000"])[0])
            except ValueError:
                max_lines = 5000
            result, total = read_log_tail(
                limit=limit, since=since,
                level=level, search=search,
                max_lines=max_lines,
            )
            # result 是 [(line_no, text), ...], 转 lines + line_nos
            lines = [t for _, t in result]
            line_nos = [n for n, _ in result]
            return json_response(self, 200, {
                "lines": lines,
                "line_nos": line_nos,
                "total": total,
                "level": level,
                "search": search,
            })

        if path == "/api/logs/download":
            # 原始文件下载
            try:
                body = SCRIPT_LOG.read_bytes() if SCRIPT_LOG.exists() else b""
            except OSError as e:
                return self.send_error(500, str(e))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="compress-{time.strftime("%Y%m%d-%H%M%S")}.log"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/files/input":
            return json_response(self, 200, {"files": list_files(
                INPUT_DIR,
                q=qs.get("q", [""])[0],
                sort=qs.get("sort", ["mtime"])[0],
                order=qs.get("order", ["desc"])[0],
                page=int(qs.get("page", ["1"])[0]),
                page_size=int(qs.get("page_size", ["0"])[0]),
            )})
        if path == "/api/files/output":
            return json_response(self, 200, {"files": list_files(
                OUTPUT_DIR,
                q=qs.get("q", [""])[0],
                sort=qs.get("sort", ["mtime"])[0],
                order=qs.get("order", ["desc"])[0],
                page=int(qs.get("page", ["1"])[0]),
                page_size=int(qs.get("page_size", ["0"])[0]),
            )})

        # ---- 视频流(支持 Range,允许跨源播放)----
        if path == "/api/files/stream":
            dir_param = qs.get("dir", ["output"])[0]
            file_param = qs.get("path", [""])[0]
            return self._stream_file(dir_param, file_param)

        if path == "/api/files/download":
            dir_param = qs.get("dir", ["output"])[0]
            file_param = qs.get("path", [""])[0]
            return self._download_file(dir_param, file_param)

        if path == "/api/files/info":
            dir_param = qs.get("dir", ["output"])[0]
            file_param = qs.get("path", [""])[0]
            base = INPUT_DIR if dir_param == "input" else OUTPUT_DIR
            fp = _safe_join(base, file_param)
            if not fp or not fp.is_file():
                return json_response(self, 404, {"ok": False, "error": "not found"})
            st = fp.stat()
            return json_response(self, 200, {
                "ok": True, "path": str(fp), "size": st.st_size,
                "mtime": int(st.st_mtime),
                "mtime_h": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            })

        # ---- 缩略图(ffmpeg 抽帧 + 缓存)----
        if path == "/api/files/thumb":
            dir_param = qs.get("dir", ["output"])[0]
            file_param = qs.get("path", [""])[0]
            return self._serve_thumbnail(dir_param, file_param)

        # ---- 播放进度条缩略图(多帧)----
        if path == "/api/pb/thumbs":
            dir_param = qs.get("dir", ["output"])[0]
            file_param = qs.get("path", [""])[0]
            count = int(qs.get("count", ["20"])[0])
            return json_response(self, 200, get_or_make_thumbs(dir_param, file_param, count))
        if path == "/api/pb/thumb":
            return self._serve_pb_thumb(qs)

        # ---- 文件日期列表(供"自动跳下一天")----
        if path == "/api/files/dates":
            dir_param = qs.get("dir", ["output"])[0]
            base = INPUT_DIR if dir_param == "input" else OUTPUT_DIR
            dates = get_file_dates(base)
            return json_response(self, 200, {"dir": dir_param, "dates": dates, "count": len(dates)})

        if path == "/api/stats":
            return json_response(self, 200, get_stats())

        if path == "/api/history":
            limit = int(qs.get("limit", ["20"])[0])
            return json_response(self, 200, {"runs": get_history(limit)})

        if path == "/api/config":
            cfg, _ = read_script_config()
            return json_response(self, 200, {"config": cfg, "keys": CONFIG_KEYS})

        # ===== 编码参数 (QP / 码率上限 / 强制重压) =====
        if path == "/api/enc-settings":
            return json_response(self, 200, {
                "qp":               _get_rkmpp_qp(),
                "bitrate_cap":      _get_rkmpp_bitrate_cap(),
                "force_recompress": _get_force_recompress(),
            })

        if path == "/api/settings":
            return json_response(self, 200, {
                "input_dir":  str(INPUT_DIR),
                "output_dir": str(OUTPUT_DIR),
                "defaults":   {
                    "input_dir":  str(_INPUT_DIR_DEFAULT),
                    "output_dir": str(_OUTPUT_DIR_DEFAULT),
                },
            })

        if path == "/api/cron":
            jobs = read_ofelia_jobs()
            for j in jobs:
                j["next_run"] = cron_next_run(j.get("schedule", ""))
            return json_response(self, 200, {"jobs": jobs, "ini_path": str(OFELIA_INI)})

        if path == "/api/system":
            ff, ver = ffmpeg_version()
            return json_response(self, 200, {
                "ffmpeg":  ff,
                "ffmpeg_version": ver,
                "hints":   detect_hwaccel_hint(),
                "input_dir":  str(INPUT_DIR),
                "output_dir": str(OUTPUT_DIR),
                "script":     str(SCRIPT_PATH),
                "script_log": str(SCRIPT_LOG),
            })

        if path == "/api/disk":
            return json_response(self, 200, disk_usage())

        # ===== 任务队列 =====
        if path == "/api/queue":
            qs = parse_qs(url.query)
            status   = (qs.get("status",   [None])[0] or None)
            sort_by  = (qs.get("sort_by",  [None])[0] or None)
            sort_dir = (qs.get("sort_dir", ["desc"])[0] or "desc")
            search   = (qs.get("q",        [None])[0] or None)
            try:
                limit  = int(qs.get("limit",  ["200"])[0])
                offset = int(qs.get("offset", ["0"])[0])
            except ValueError:
                limit, offset = 200, 0
            items, total = list_tasks(
                status=status, limit=limit, offset=offset,
                sort_by=sort_by, sort_dir=sort_dir, search=search,
            )
            return json_response(self, 200, {
                "items": items, "total": total,
                "limit": limit, "offset": offset,
                "status": status, "sort_by": sort_by, "sort_dir": sort_dir,
                "search": search,
            })

        if path == "/api/queue/stats":
            return json_response(self, 200, get_queue_stats())

        # ===== schedules GET =====
        if path == "/api/schedules":
            return json_response(self, 200, {"schedules": list_schedules()})

        # ===== cluster GET =====
        if path == "/api/cluster/state":
            return json_response(self, 200, get_self_state())

        if path == "/api/cluster/peers":
            return json_response(self, 200, {
                "self": {
                    "id": get_self_id(),
                    "name": get_self_name(),
                    "url": get_self_url(),
                    "state": get_self_state(),
                },
                "peers": list(_cluster_cache["peers"].values()),
                "last_refresh": _cluster_cache.get("last_refresh"),
            })

        if path == "/api/cluster/nodes":
            # 集群管理面板用的精简视图:返回 settings 里的源数据 + 每节点最新状态
            # 与 /api/cluster/peers 不同:不走缓存,直读 DB,客户端可立即拿到刚保存的数据
            raw = _get_setting("cluster.peers", "[]")
            try:
                cfg = json.loads(raw) if raw else []
            except Exception:
                cfg = []
            nodes = []
            for p in cfg:
                pid = p.get("id") or p.get("url")
                cached = _cluster_cache["peers"].get(pid, {})
                nodes.append({
                    "id": pid,
                    "name": p.get("name", pid),
                    "url": p.get("url", "").rstrip("/"),
                    "ok": cached.get("ok", False),
                    "online": cached.get("ok", False),
                    "last_fetched": cached.get("fetched_at"),
                    "state": (cached.get("state") or {}),
                })
            return json_response(self, 200, {
                "nodes": nodes,
                "self": {
                    "id": get_self_id(),
                    "name": get_self_name(),
                    "url": get_self_url(),
                },
                "source": "settings:cluster.peers",
            })

        if path == "/api/cluster/health":
            return json_response(self, 200, {
                "ok": True,
                "version": "refactor/1",
                "peers": len(_cluster_cache["peers"]),
                "service": "video-manager",
            })

        if path == "/api/cluster/version":
            # 供子节点 auto_update 用 — 返回 master 的 head commit
            try:
                commit = subprocess.check_output(
                    ["git", "-C", str(APP_DIR), "rev-parse", "HEAD"],
                    stderr=subprocess.DEVNULL, text=True
                ).strip()
            except Exception:
                commit = "unknown"
            return json_response(self, 200, {
                "ok": True,
                "version": "refactor/1",
                "commit": commit,
                "service": "video-manager",
                "auto_update": _get_setting("cluster.auto_update", "1"),
            })

        if path == "/api/cluster/files":
            return json_response(self, 200,
                _cluster_aggregate_files(
                    qs.get("dir", ["output"])[0],
                    q=qs.get("q", [""])[0],
                    sort=qs.get("sort", ["mtime"])[0],
                    order=qs.get("order", ["desc"])[0],
                    page=int(qs.get("page", ["1"])[0]),
                    page_size=int(qs.get("page_size", ["0"])[0]),
                ))

        # ---- 代理:透过本节点转播远端 peer 的视频流(处理 HTTPS Mixed Content)----
        if path == "/api/cluster/stream":
            return self._proxy_peer_stream(qs, as_attachment=False)

        if path == "/api/cluster/download":
            return self._proxy_peer_stream(qs, as_attachment=True)

        if path == "/api/cron/status":
            # 检查 ofelia 容器状态
            state = "unknown"
            try:
                r = subprocess.run(
                    ["docker", "ps", "-a", "--filter", "name=ofelia-scheduler",
                     "--format", "{{.Names}} {{.State}}"],
                    capture_output=True, text=True, timeout=3,
                )
                line = r.stdout.strip()
                if not line:
                    state = "absent"
                else:
                    parts = line.split()
                    state = parts[1] if len(parts) > 1 else "unknown"
            except (FileNotFoundError, subprocess.TimeoutExpired):
                state = "docker_unavailable"
            except Exception as e:
                state = f"error:{e}"
            return json_response(self, 200, {"state": state})

        return self.send_error(404)

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        if path == "/api/run":
            ok, msg = start_run(trigger=data.get("trigger", "manual"))
            return json_response(self, 200, {"ok": ok, "message": msg, "state": get_state()})

        if path == "/api/stop":
            ok, msg = stop_run()
            return json_response(self, 200, {"ok": ok, "message": msg, "state": get_state()})

        if path == "/api/config":
            ok = update_script_config(data.get("config", {}))
            cfg, _ = read_script_config()
            return json_response(self, 200, {"ok": ok, "config": cfg})

        if path == "/api/settings":
            ok, msg, result = update_settings(data)
            if not ok:
                return json_response(self, 400, {"ok": False, "error": msg})
            return json_response(self, 200, {"ok": True, "msg": msg, **result})

        # ===== 编码参数 POST =====
        if path == "/api/enc-settings":
            note = []
            if "qp" in data:
                qp = int(data.get("qp", 28))
                qp = max(18, min(36, qp))
                _set_setting("rkmpp_qp", str(qp))
                note.append(f"QP={qp}")
            if "bitrate_cap" in data:
                cap = max(0, int(data.get("bitrate_cap", 4000)))
                _set_setting("rkmpp_bitrate_cap", str(cap))
                note.append(f"码率上限={cap if cap else '无限'}kbps")
            if "force_recompress" in data:
                v = bool(data.get("force_recompress"))
                _set_setting("force_recompress", "1" if v else "0")
                note.append("强制重压=ON" if v else "强制重压=OFF")
            return json_response(self, 200, {"ok": True, "note": " · ".join(note) or "无变更"})

        if path == "/api/service/restart":
            # 重启当前 video-manager 服务本身。
            # 需要 kxrdyf 能 NOPASSWD 跑 systemctl restart video-manager。
            # 顺序: 先把响应 flush 给客户端 -> 后台线程 sleep 一下 -> Popen systemctl
            #       (systemd 会 SIGTERM 当前进程，但客户端已经拿到 200)
            payload = json.dumps({"ok": True, "msg": "重启指令已发送，服务约 1-3 秒后恢复"}).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
            except Exception as e:
                log(f"重启响应 flush 失败: {e}", level=logging.WARNING)
                return

            def _trigger():
                time.sleep(0.2)  # 客户端先拿到响应
                try:
                    p = subprocess.Popen(
                        ["sudo", "-n", "/usr/bin/systemctl", "restart", "video-manager"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                    )
                    _, err = p.communicate(timeout=15)
                    if p.returncode != 0:
                        log(f"systemctl restart 失败 (rc={p.returncode}): {err.decode(errors='replace')}",
                            level=logging.ERROR)
                except subprocess.TimeoutExpired:
                    log("systemctl restart 超时", level=logging.ERROR)
                except Exception as e:
                    log(f"重启服务异常: {e}", level=logging.ERROR)
            threading.Thread(target=_trigger, daemon=True).start()
            return  # 不再走下面的 404 分支

        if path == "/api/cron":
            jobs = data.get("jobs", [])
            ok = update_ofelia_jobs(jobs)
            new_jobs = read_ofelia_jobs()
            for j in new_jobs:
                j["next_run"] = cron_next_run(j.get("schedule", ""))
            return json_response(self, 200, {"ok": ok, "jobs": new_jobs})

        if path == "/api/cron/restart":
            ok, msg = restart_ofelia()
            return json_response(self, 200, {"ok": ok, "message": msg})

        # ---- 文件管理 ----
        if path == "/api/files/delete":
            dir_param = (data.get("dir") or "").strip()
            file_param = (data.get("path") or "").strip()
            base = INPUT_DIR if dir_param == "input" else OUTPUT_DIR
            if not dir_param or not file_param:
                return json_response(self, 400, {"ok": False, "error": "需要 dir 和 path"})
            fp = self._safe_join(base, file_param)
            if not fp or not fp.is_file():
                return json_response(self, 404, {"ok": False, "error": "未找到"})
            try:
                size = fp.stat().st_size
                fp.unlink()
                log(f"已删除文件: {dir_param}/{file_param} ({size} bytes)")
                return json_response(self, 200, {"ok": True, "size": size})
            except Exception as e:
                return json_response(self, 500, {"ok": False, "error": str(e)})

        # ---- 集群文件聚合 ----
        if path == "/api/cluster/files":
            dir_param = (data.get("dir") if data else None) or qs.get("dir", ["output"])[0]
            return json_response(self, 200, _cluster_aggregate_files(
                dir_param,
                q=(data.get("q") if data else None) or qs.get("q", [""])[0],
                sort=(data.get("sort") if data else None) or qs.get("sort", ["mtime"])[0],
                order=(data.get("order") if data else None) or qs.get("order", ["desc"])[0],
                page=int((data.get("page") if data else None) or qs.get("page", ["1"])[0]),
                page_size=int((data.get("page_size") if data else None) or qs.get("page_size", ["0"])[0]),
            ))

        # ---- 集群:代理远端 peer 的控制接口 (run / stop / service_restart / sync_tasks) ----
        m_peer_control = re.match(r"^/api/cluster/(?:run|stop|service_restart|service/restart|queue/sync|queue/retry|schedule/fire|schedule/upsert)$", path)
        if m_peer_control:
            # peer_id 必填(body 里)
            peer_id = (data.get("peer_id") or qs.get("peer_id", [""])[0]).strip()
            if not peer_id:
                return json_response(self, 400, {"ok": False, "error": "需要 peer_id 参数"})
            # 查 peer URL(在 settings:cluster.peers 或者 _cluster_cache.peers)
            raw = _get_setting("cluster.peers", "[]")
            try:
                cfg = json.loads(raw) if raw else []
            except Exception:
                cfg = []
            peer = next((p for p in cfg if p.get("id") == peer_id), None)
            if not peer:
                # 尝试 cache 的 id
                cached = _cluster_cache["peers"].get(peer_id, {})
                peer_url = cached.get("url")
                if not peer_url:
                    return json_response(self, 404, {"ok": False, "error": f"peer {peer_id!r} 不存在"})
            else:
                peer_url = (peer.get("url") or "").rstrip("/")
            # 前拼到 peer_url,path 后面的改成去掉 /api/cluster/ 前缀
            target_path = path.replace("/api/cluster/", "/api/")
            target_url = f"{peer_url}{target_path}"
            try:
                body = json.dumps(data).encode("utf-8")
                req = _urlreq.Request(
                    target_url, data=body, method="POST",
                    headers={"Content-Type": "application/json", "User-Agent": "video-manager-cluster/1.0"}
                )
                with _urlreq.urlopen(req, timeout=15) as r:
                    body_b = r.read()
                    try:
                        payload = json.loads(body_b)
                    except Exception:
                        payload = {"raw": body_b.decode("utf-8", errors="replace")}
                    return json_response(self, 200, {"ok": True, "via": peer_id, "result": payload})
            except Exception as e:
                log(f"cluster proxy to {peer_id} failed: {e}", level=logging.WARNING)
                return json_response(self, 502, {"ok": False, "error": f"peer {peer_id} unreachable: {e}"})

        # ---- 集群节点对远端文件的代理操作(删除/下载)----
        if path == "/api/cluster/file_action":
            peer_url = (data.get("peer_url") or "").rstrip("/")
            action = (data.get("action") or "").strip()  # "delete" | "info"
            file_path = (data.get("path") or "").strip()
            dir_param = (data.get("dir") or "output").strip()
            if not peer_url or not action or not file_path:
                return json_response(self, 400, {"ok": False, "error": "需要 peer_url, action, path"})
            try:
                target = f"{peer_url}/api/files/{action}"
                req_data = json.dumps({"dir": dir_param, "path": file_path}).encode()
                req = _urlreq.Request(target, data=req_data if action == "delete" else None,
                                       method="POST",
                                       headers={"Content-Type": "application/json"})
                with _urlreq.urlopen(req, timeout=10) as r:
                    body = r.read().decode("utf-8")
                    return json_response(self, 200, json.loads(body))
            except Exception as e:
                return json_response(self, 500, {"ok": False, "error": str(e)})

        # ===== schedules POST =====
        if path == "/api/schedules/upsert":
            payload = data.get("trigger_payload", {"trigger": "cron"})
            ok, msg, new_id = upsert_schedule({
                "id": data.get("id"),
                "name": (data.get("name") or "").strip(),
                "cron_expr": (data.get("cron_expr") or "").strip(),
                "enabled": bool(data.get("enabled", True)),
                "trigger_payload": payload,
            })
            if not ok:
                return json_response(self, 400, {"ok": False, "error": msg})
            return json_response(self, 200, {"ok": True, "id": new_id})

        if path == "/api/schedules/delete":
            sid = data.get("id")
            if not sid or not delete_schedule(sid):
                return json_response(self, 404, {"ok": False, "error": "未找到"})
            return json_response(self, 200, {"ok": True})

        if path == "/api/schedules/fire":
            sid = data.get("id")
            if not sid:
                return json_response(self, 400, {"ok": False, "error": "需要 id"})
            ok, msg, _ = fire_schedule(sid)
            return json_response(self, 200, {"ok": ok, "message": msg})

        if path == "/api/schedules/preview":
            expr = (data.get("cron_expr") or "").strip()
            try:
                now = datetime.now()
                runs = []
                cur = now - timedelta(minutes=1)
                for _ in range(3):
                    cur = _next_run_time(expr, cur)
                    runs.append(cur.strftime("%Y-%m-%d %H:%M:%S"))
                return json_response(self, 200, {"ok": True, "runs": runs})
            except Exception as e:
                return json_response(self, 400, {"ok": False, "error": str(e)})

        # ===== cluster POST =====
        if path == "/api/cluster/peers/upsert":
            peers = data.get("peers", [])
            if not isinstance(peers, list):
                return json_response(self, 400, {"ok": False, "error": "peers 必须是数组"})
            cleaned = update_peers(peers)
            return json_response(self, 200, {"ok": True, "peers": cleaned})

        if path == "/api/cluster/self/update":
            update_self(
                sid=data.get("id"),
                sname=data.get("name"),
                surl=data.get("url"),
            )
            return json_response(self, 200, {"ok": True, **get_self_state()})

        if path == "/api/cluster/refresh":
            _cluster_refresh_all()
            return json_response(self, 200, {
                "ok": True,
                "last_refresh": _cluster_cache.get("last_refresh"),
                "peers": list(_cluster_cache["peers"].values()),
            })

        # ===== cluster POST: 单节点增删改(给 install.sh join 用)=====
        m_peer = re.match(r"^/api/cluster/peers/([A-Za-z0-9_-]{1,32})/?$", path)
        if m_peer:
            peer_id = m_peer.group(1)
            return self._handle_peer_op(peer_id, data)

        # ===== 任务队列 POST =====
        if path == "/api/queue/sync":
            try:
                r = sync_tasks_from_input()
                return json_response(self, 200, {"ok": True, "synced": r, "stats": get_queue_stats()})
            except Exception as e:
                return json_response(self, 500, {"ok": False, "error": str(e)})

        if path == "/api/queue/retry":
            ids = data.get("ids", [])
            if not isinstance(ids, list):
                return json_response(self, 400, {"ok": False, "error": "ids must be list"})
            try:
                ids = [int(x) for x in ids]
            except (TypeError, ValueError):
                return json_response(self, 400, {"ok": False, "error": "ids must be integers"})
            try:
                r = retry_tasks(ids)
                return json_response(self, 200, {"ok": True, "result": r, "stats": get_queue_stats()})
            except Exception as e:
                return json_response(self, 500, {"ok": False, "error": str(e)})

        if path == "/api/queue/backfill_durations":
            try:
                result = backfill_task_durations()
                return json_response(self, 200, {"ok": True, "result": result})
            except Exception as e:
                return json_response(self, 500, {"ok": False, "error": str(e)})

        if path == "/api/queue/delete":
            ids = data.get("ids", [])
            if not isinstance(ids, list):
                return json_response(self, 400, {"ok": False, "error": "ids must be list"})
            try:
                ids = [int(x) for x in ids]
            except (TypeError, ValueError):
                return json_response(self, 400, {"ok": False, "error": "ids must be integers"})
            try:
                r = delete_tasks(ids)
                return json_response(self, 200, {"ok": True, "result": r, "stats": get_queue_stats()})
            except Exception as e:
                return json_response(self, 500, {"ok": False, "error": str(e)})

        return self.send_error(404)

# ============== 任务队列(tasks 表) ==============
QUEUE_STATUSES = ("pending", "running", "done", "failed", "skipped")

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

def sync_tasks_from_input() -> dict:
    """从 /input 和 /output 扫描,同步 tasks 表。返回本次新增/更新的统计。"""
    added_input = added_done = updated_done = reconciled = 0
    # 先一轮走,把 /input 和 /output 里的 rel_path 收齐
    input_seen: set[str]  = set()
    output_seen: set[str] = set()
    for rel, _full, size in _walk_mp4(INPUT_DIR):
        input_seen.add(rel)
    for rel, _full, size in _walk_mp4(OUTPUT_DIR):
        output_seen.add(rel)

    with db() as conn, _db_lock:
        # 1. /input 里在的 -> pending(如果不存在或不是 done)
        for rel in input_seen:
            row = conn.execute(
                "SELECT id, status FROM tasks WHERE rel_path=?", (rel,)
            ).fetchone()
            if row is None:
                # 拿不到 size (上面的扫描丢了),读 -1 占位
                try:
                    size = (INPUT_DIR / rel).stat().st_size
                except OSError:
                    size = 0
                conn.execute(
                    "INSERT INTO tasks(rel_path, size, status) VALUES(?, ?, 'pending')",
                    (rel, size),
                )
                added_input += 1
            elif row["status"] in ("done", "skipped"):
                # 保持原状态
                pass
        # 2. /output 里有 -> done(如果还没标记)
        for rel in output_seen:
            row = conn.execute(
                "SELECT id, status FROM tasks WHERE rel_path=?", (rel,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO tasks(rel_path, size, status) VALUES(?, ?, 'done')",
                    (rel, 0),
                )
                added_done += 1
            elif row["status"] not in ("done",):
                conn.execute(
                    "UPDATE tasks SET status='done', ended_at=COALESCE(ended_at,?) WHERE id=?",
                    (now_str(), row["id"]),
                )
                updated_done += 1
        # 3. 调和:既不在 /input 也不在 /output 的 pending/running 任务 → skipped
        # (说明文件已经被外部删了——常见: 旧 bash 处理过、用户手动 rm、清理例行任务)
        rows = conn.execute(
            "SELECT id, rel_path FROM tasks WHERE status IN ('pending','running')"
        ).fetchall()
        for r in rows:
            if r["rel_path"] not in input_seen and r["rel_path"] not in output_seen:
                conn.execute(
                    "UPDATE tasks SET status='skipped', "
                    "ended_at=?, "
                    "last_error=COALESCE(last_error,'reconciled: file gone from both input and output') "
                    "WHERE id=?",
                    (now_str(), r["id"]),
                )
                reconciled += 1
        conn.commit()
    return {
        "added_input":  added_input,
        "added_done":   added_done,
        "updated_done": updated_done,
        "reconciled":   reconciled,
    }

def get_queue_stats() -> dict:
    with db() as conn, _db_lock:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
    counts = {s: 0 for s in QUEUE_STATUSES}
    for r in rows:
        counts[r["status"]] = r["n"]
    total = sum(counts.values())
    return {**counts, "total": total}

# 允许排序的列(防 SQL 注入)
_SORTABLE_COLS = {
    "id", "rel_path", "size", "output_size",
    "attempts", "status", "ended_at",
    "duration_sec", "ratio",   # 表达式列,在 SELECT 里定义
}

def list_tasks(status=None, limit=200, offset=0,
               sort_by=None, sort_dir="desc", search=None):
    if status and status not in QUEUE_STATUSES:
        return [], 0
    if sort_by not in _SORTABLE_COLS:
        sort_by = None
    sort_dir = "desc" if sort_dir not in ("asc", "desc") else sort_dir

    # WHERE 构造
    where_clauses = []
    where_params  = []
    if status and status in QUEUE_STATUSES:
        where_clauses.append("status=?")
        where_params.append(status)
    if search:
        where_clauses.append("rel_path LIKE ?")
        where_params.append(f"%{search}%")
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # ORDER BY
    if sort_by is None:
        order_sql = """ORDER BY
            CASE status
                WHEN 'running' THEN 0
                WHEN 'failed'  THEN 1
                WHEN 'pending' THEN 2
                WHEN 'skipped' THEN 3
                ELSE 4
            END,
            id DESC"""
    else:
        dir_sql = "DESC" if sort_dir == "desc" else "ASC"
        # NULL 值统一排到末尾
        order_sql = f"""ORDER BY
            CASE WHEN {sort_by} IS NULL THEN 1 ELSE 0 END,
            {sort_by} {dir_sql}, id DESC"""

    # 用 SQL 计算 duration_sec 和 ratio,方便 ORDER BY 使用
    select_sql = """SELECT id, rel_path, size, output_size, status, attempts,
                            last_error, last_run_id,
                            created_at, started_at, ended_at,
                            CASE WHEN started_at IS NULL OR ended_at IS NULL
                                 THEN NULL
                                 ELSE CAST((julianday(ended_at) - julianday(started_at)) * 86400 AS INTEGER)
                            END AS duration_sec,
                            CASE WHEN size IS NULL OR size = 0 OR output_size IS NULL
                                 THEN NULL
                                 ELSE ROUND(output_size * 1.0 / size, 3)
                            END AS ratio
                     FROM tasks"""

    with db() as conn, _db_lock:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM tasks {where_sql}", where_params
        ).fetchone()["n"]
        rows = conn.execute(
            f"{select_sql} {where_sql} {order_sql} LIMIT ? OFFSET ?",
            where_params + [limit, offset],
        ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        # ratio 可能是 0(空表达式), 也允许
        items.append(d)
    return items, total

def retry_tasks(ids: list) -> dict:
    """重试指定任务: 删除对应 /output 文件,重置状态为 pending。"""
    if not ids:
        return {"reset": 0, "deleted_outputs": 0, "not_found": 0}
    reset = deleted = 0
    not_found = []
    with db() as conn, _db_lock:
        for tid in ids:
            row = conn.execute(
                "SELECT id, rel_path, status FROM tasks WHERE id=?", (tid,)
            ).fetchone()
            if row is None:
                not_found.append(tid)
                continue
            output_file = OUTPUT_DIR / row["rel_path"]
            try:
                if output_file.exists():
                    output_file.unlink()
                    deleted += 1
            except OSError as e:
                log(f"retry_tasks: 删除输出失败 {output_file}: {e}")
            conn.execute(
                """UPDATE tasks SET status='pending', attempts=0,
                                     last_error=NULL,
                                     started_at=NULL, ended_at=NULL
                   WHERE id=?""",
                (row["id"],),
            )
            reset += 1
        conn.commit()
    return {"reset": reset, "deleted_outputs": deleted, "not_found": len(not_found)}

def backfill_task_durations() -> dict:
    """回填历史 done 任务的 started_at / ended_at。

    旧 bash 管线处理的 /output 文件被首次 sync_tasks_from_input() 导入时,
    /output 已存在 → status='done' + ended_at=sync_time, 但真实压缩开始时间
    完全没记录。导致 UI 队列的「用时」列对 ~3000 个历史任务显示「—」。

    思路:
      - 用 /output mtime 作为真实 ended_at(压缩完成时刻)
      - started_at = ended_at - 估算时长;估算时长 = clamp(size/throughput, 30s, 30min)
      - throughput 从已完成任务的真实数据中位数算出 (≈1.1 MB/s, libx264)
    对 idle 异常(duration < 30s 或 > 30min)的也重写。
    Idempotent: WHERE started_at IS NULL 只补一次,后续靠时长区间修正。
    """
    from datetime import datetime as _dt
    with db() as conn, _db_lock:
        row = conn.execute("""
            SELECT AVG(size * 1.0 / CAST((julianday(ended_at) - julianday(started_at)) * 86400 AS INTEGER)) AS bps,
                   COUNT(*) AS n
            FROM tasks
            WHERE status='done' AND started_at IS NOT NULL AND ended_at IS NOT NULL
              AND size > 0
              AND CAST((julianday(ended_at) - julianday(started_at)) * 86400 AS INTEGER) BETWEEN 30 AND 1800
        """).fetchone()
        bps = row["bps"] if row["bps"] and row["bps"] > 0 else 1_000_000
        log(f"backfill_durations: 吞吐率 {bps/1024/1024:.2f} MB/s (样本 {row['n']} 个)")

        # 候选 1:started_at IS NULL 的 done 任务
        targets = conn.execute("""
            SELECT id, rel_path, size FROM tasks
            WHERE status='done' AND started_at IS NULL
        """).fetchall()
        # 候选 2:duration 异常的 done 任务(< 30s 或 > 30min)
        bad = conn.execute("""
            SELECT id, rel_path, size FROM tasks
            WHERE status='done' AND started_at IS NOT NULL AND ended_at IS NOT NULL
              AND (CAST((julianday(ended_at) - julianday(started_at)) * 86400 AS INTEGER) < 30
                   OR CAST((julianday(ended_at) - julianday(started_at)) * 86400 AS INTEGER) > 1800)
        """).fetchall()

    all_rows = targets + bad
    fixed_null = fixed_bad = no_output = 0
    seen_ids = set()
    for tid, rel, size in all_rows:
        if tid in seen_ids:
            continue
        seen_ids.add(tid)
        out = OUTPUT_DIR / rel
        if not out.exists():
            no_output += 1
            continue
        try:
            mtime = out.stat().st_mtime
        except OSError:
            continue
        if size and size > 0:
            duration = int(size / bps)
        else:
            duration = 120
        duration = max(30, min(duration, 1800))
        start_ts = mtime - duration
        ended_at   = _dt.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        started_at = _dt.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M:%S")
        with db() as conn2, _db_lock:
            cur = conn2.execute(
                "UPDATE tasks SET started_at=?, ended_at=? WHERE id=?",
                (started_at, ended_at, tid),
            )
            conn2.commit()
            if cur.rowcount > 0:
                if tid in {r[0] for r in targets}:
                    fixed_null += 1
                else:
                    fixed_bad += 1
    log(f"backfill_durations: fixed_null={fixed_null} fixed_bad={fixed_bad} no_output={no_output}")
    return {
        "fixed_null": fixed_null,
        "fixed_bad":  fixed_bad,
        "no_output":  no_output,
        "bytes_per_sec": bps,
    }

def delete_tasks(ids: list) -> dict:
    """删除指定 tasks 记录（仅删除表行,不动 /input /output 文件）。"""
    if not ids:
        return {"deleted": 0, "not_found": 0, "rejected": 0}
    deleted = not_found = rejected = 0
    rejected_ids = []
    with db() as conn, _db_lock:
        for tid in ids:
            row = conn.execute(
                "SELECT id, status FROM tasks WHERE id=?", (tid,)
            ).fetchone()
            if row is None:
                not_found += 1
                continue
            if row["status"] == "running":
                # 拒绝删除正在跑的任务(避免中断 worker)
                rejected += 1
                rejected_ids.append(tid)
                continue
            conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
            deleted += 1
        conn.commit()
    return {
        "deleted": deleted,
        "not_found": not_found,
        "rejected": rejected,
        "rejected_ids": rejected_ids,
    }

# ============== 文件列表 ==============
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

# 缩略图缓存目录
THUMB_DIR = APP_DIR / "data" / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
PB_THUMB_DIR = APP_DIR / "data" / "pb_thumbs"
PB_THUMB_DIR.mkdir(parents=True, exist_ok=True)

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
    base = INPUT_DIR if dir_name == "input" else OUTPUT_DIR
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
    base = INPUT_DIR if dir_name == "input" else OUTPUT_DIR
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
    for label, p in [("input", INPUT_DIR), ("output", OUTPUT_DIR), ("scripts", SCRIPT_PATH.parent)]:
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

# ============== 统计 ==============
def get_stats():
    out = {"today": {}, "total": {}, "recent": []}
    with db() as conn, _db_lock:
        for window, label in [("today", "今日"), ("all", "总计")]:
            if window == "today":
                where = "WHERE date(started_at) = date('now','localtime') AND ended_at IS NOT NULL"
            else:
                where = "WHERE ended_at IS NOT NULL"
            r = conn.execute(f"""
                SELECT COUNT(*) AS runs,
                       COALESCE(SUM(success),0) AS success,
                       COALESCE(SUM(skipped),0) AS skipped,
                       COALESCE(SUM(failed),0)  AS failed,
                       COALESCE(SUM(total),0)   AS total
                FROM runs {where}
            """).fetchone()
            out["today" if window=="today" else "total"] = {
                "runs":    r["runs"],
                "success": r["success"],
                "skipped": r["skipped"],
                "failed":  r["failed"],
                "total":   r["total"],
            }
        out["recent"] = [dict(row) for row in conn.execute(
            "SELECT id, started_at, ended_at, trigger, success, skipped, failed, total FROM runs ORDER BY id DESC LIMIT 10"
        ).fetchall()]
    return out

def get_history(limit=20):
    with db() as conn, _db_lock:
        rows = conn.execute(
            "SELECT id, started_at, ended_at, trigger, success, skipped, failed, total FROM runs ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

# ============== Settings（路径配置，热加载） ==============
_settings_lock = threading.Lock()

def _settings_table():
    """确保 settings 表存在。"""
    with db() as conn, _db_lock:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
    return conn

def _read_setting(key: str, default: Path) -> Path:
    try:
        with _settings_table() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row and row["value"]:
                p = Path(row["value"])
                if p.is_absolute():
                    return p
    except Exception as e:
        log(f"读 settings[{key}] 失败: {e}", level=logging.WARNING)
    return default

def load_settings():
    """启动时调用:从 DB 加载 INPUT_DIR/OUTPUT_DIR;首次运行把默认值落库。"""
    global INPUT_DIR, OUTPUT_DIR
    with _settings_lock:
        INPUT_DIR  = _read_setting("input_dir",  _INPUT_DIR_DEFAULT)
        OUTPUT_DIR = _read_setting("output_dir", _OUTPUT_DIR_DEFAULT)
        try:
            with _settings_table() as conn:
                conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                             ("input_dir", str(INPUT_DIR)))
                conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                             ("output_dir", str(OUTPUT_DIR)))
        except Exception as e:
            log(f"settings 默认值写库失败: {e}", level=logging.WARNING)
    log(f"加载路径: input={INPUT_DIR}  output={OUTPUT_DIR}")

# 禁止选择的路径前缀（系统 / 挂载点目录，容易误选）
_BAD_PATH_PREFIXES = (
    "/proc", "/sys", "/dev", "/run", "/boot", "/etc", "/var", "/usr",
    "/lib", "/lib64", "/bin", "/sbin", "/opt", "/srv", "/mnt", "/media",
    "/tmp", "/root", "/home",
    str(APP_DIR),                  # 不能把输出指到 app 自身目录里
)

def _validate_path(p_str: str, *, must_be_writable=False, must_be_readable=True, allow_create=True):
    """返回 (ok, msg, resolved|None)。"""
    if not p_str or not str(p_str).strip():
        return False, "路径不能为空", None
    raw = str(p_str).strip()
    if not raw.startswith("/"):
        return False, "必须使用绝对路径（以 / 开头）", None
    try:
        resolved = Path(raw).expanduser().resolve()
    except Exception as e:
        return False, f"路径解析失败: {e}", None
    resolved_s = str(resolved)
    for bp in _BAD_PATH_PREFIXES:
        if resolved_s == bp or resolved_s.startswith(bp.rstrip("/") + "/"):
            return False, f"禁止使用 {bp} 下的路径", None
    if not resolved.exists():
        if not allow_create:
            return False, f"目录不存在: {resolved}", None
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            return False, f"目录不存在且无权限创建: {resolved}", None
        except Exception as e:
            return False, f"目录不存在且无法创建: {e}", None
    if not resolved.is_dir():
        return False, f"不是目录: {resolved}", None
    if must_be_readable and not os.access(resolved, os.R_OK):
        return False, f"目录不可读: {resolved}", None
    if must_be_writable and not os.access(resolved, os.W_OK):
        return False, f"目录不可写: {resolved}", None
    return True, "OK", resolved

def update_settings(updates: dict):
    """更新路径设置。返回 (ok, msg, dict|None)。"""
    global INPUT_DIR, OUTPUT_DIR
    new_in_raw  = str(updates.get("input_dir",  str(INPUT_DIR))).strip()
    new_out_raw = str(updates.get("output_dir", str(OUTPUT_DIR))).strip()
    ok1, m1, p1 = _validate_path(new_in_raw,  must_be_writable=False, must_be_readable=True)
    if not ok1:
        return False, f"input_dir: {m1}", None
    ok2, m2, p2 = _validate_path(new_out_raw, must_be_writable=True,  must_be_readable=True)
    if not ok2:
        return False, f"output_dir: {m2}", None
    if p1 == p2:
        return False, "输入和输出不能是同一目录", None
    in_changed  = p1 != INPUT_DIR
    out_changed = p2 != OUTPUT_DIR
    with _settings_lock:
        with _settings_table() as conn:
            for k, v in [("input_dir", str(p1)), ("output_dir", str(p2))]:
                conn.execute(
                    "INSERT INTO settings(key,value,updated_at) VALUES(?,?,datetime('now','localtime')) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (k, v)
                )
        INPUT_DIR  = p1
        OUTPUT_DIR = p2
    log(f"路径设置更新: input={INPUT_DIR}  output={OUTPUT_DIR}")
    # 自动重新扫描新输入目录
    try:
        sync_tasks_from_input()
        log("已重新扫描输入目录,新文件加入队列")
    except Exception as e:
        log(f"sync_tasks_from_input 失败: {e}", level=logging.WARNING)
    return True, "OK", {
        "input_dir":     str(INPUT_DIR),
        "output_dir":    str(OUTPUT_DIR),
        "input_changed": in_changed,
        "output_changed": out_changed,
    }

# ============== Scheduler（UI 定时的后台调度器） ==============
_scheduler_stop = threading.Event()

def _parse_cron_field(field: str, min_v: int, max_v: int) -> set:
    """解析 cron 单个字段。支持 *, n, n-m, */n, n-m/s, 逗号分隔。"""
    result = set()
    for part in field.split(','):
        step = 1
        if '/' in part:
            part, step_str = part.split('/', 1)
            step = int(step_str)
        if part == '*' or part == '':
            start, end = min_v, max_v
        elif '-' in part:
            start, end = map(int, part.split('-', 1))
        else:
            result.add(int(part))
            continue
        for v in range(start, end + 1, step):
            result.add(v)
    return result

def _next_run_time(expr: str, after: datetime) -> datetime:
    """返回 cron expr 在 after 之后的下一次运行时间。"""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron 表达式需要 5 个字段: {expr!r}")
    mins = _parse_cron_field(parts[0], 0, 59)
    hrs  = _parse_cron_field(parts[1], 0, 23)
    doms = _parse_cron_field(parts[2], 1, 31)
    mons = _parse_cron_field(parts[3], 1, 12)
    dows = _parse_cron_field(parts[4], 0, 6)
    cur = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 12):  # 最多找一年
        if cur.month not in mons:
            cur = (cur.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0)
            continue
        if cur.day not in doms:
            cur = (cur + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        cron_dow = (cur.weekday() + 1) % 7  # Python: Mon=0 → cron: Sun=0
        if cron_dow not in dows:
            cur = (cur + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if cur.hour not in hrs:
            cur = (cur + timedelta(hours=1)).replace(minute=0)
            continue
        if cur.minute not in mins:
            cur += timedelta(minutes=1)
            continue
        return cur
    raise ValueError(f"找不到下次运行时间: {expr!r}")

def _scheduler_tick():
    now = datetime.now().replace(second=0, microsecond=0)
    with db() as conn, _db_lock:
        schedules = [dict(r) for r in conn.execute(
            "SELECT * FROM schedules WHERE enabled=1"
        )]
    for s in schedules:
        try:
            last_run_str = s.get('last_run')
            if last_run_str:
                last_run = datetime.fromisoformat(last_run_str)
                nxt = _next_run_time(s['cron_expr'], last_run)
            else:
                # 从未跑过:看过去一小时内有没有应该触发的
                nxt = _next_run_time(s['cron_expr'], now - timedelta(hours=1))
            if nxt <= now:
                payload = json.loads(s.get('trigger_payload') or '{"trigger":"cron"}')
                trigger = payload.get('trigger', 'cron')
                ok, msg = start_run(trigger=trigger)
                status = 'fired' if ok else f'failed: {msg}'
                with db() as conn, _db_lock:
                    conn.execute(
                        "UPDATE schedules SET last_run=?, last_status=? WHERE id=?",
                        (now_str(), status, s['id'])
                    )
                log(f"scheduler fired: id={s['id']} name={s['name']!r} cron={s['cron_expr']!r} -> {msg}")
        except Exception as e:
            log(f"scheduler error on id={s.get('id')}: {e}", level=logging.WARNING)

def _scheduler_loop():
    while not _scheduler_stop.is_set():
        try:
            _scheduler_tick()
        except Exception as e:
            log(f"scheduler tick error: {e}", level=logging.WARNING)
        _scheduler_stop.wait(30)  # 每 30 秒检查一次

def start_scheduler():
    """启动后台调度线程（只启动一次）"""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    _scheduler_thread.start()
    log("scheduler thread started")

def list_schedules():
    with db() as conn:
        rows = conn.execute("SELECT * FROM schedules ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # 计算下次运行
        try:
            last = datetime.fromisoformat(d['last_run']) if d.get('last_run') else None
            anchor = last or (datetime.now() - timedelta(days=1))
            d['next_run'] = _next_run_time(d['cron_expr'], anchor).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            d['next_run'] = None
        out.append(d)
    return out

def upsert_schedule(s: dict):
    """创建或更新。s 里有 id 则更新,否则新建。"""
    import uuid
    s = dict(s)
    s['id'] = s.get('id') or f"sch-{uuid.uuid4().hex[:8]}"
    enabled = 1 if s.get('enabled', True) else 0
    trigger_payload = s.get('trigger_payload') or '{"trigger":"cron"}'
    if isinstance(trigger_payload, dict):
        trigger_payload = json.dumps(trigger_payload)
    # 验证 cron
    try:
        _next_run_time(s['cron_expr'], datetime.now())
    except Exception as e:
        return False, f"cron 表达式无效: {e}", None
    with db() as conn, _db_lock:
        existing = conn.execute("SELECT id FROM schedules WHERE id=?", (s['id'],)).fetchone()
        if existing:
            conn.execute("""
                UPDATE schedules SET name=?, cron_expr=?, enabled=?, trigger_payload=?, updated_at=?
                WHERE id=?
            """, (s['name'], s['cron_expr'], enabled, trigger_payload, now_str(), s['id']))
        else:
            conn.execute("""
                INSERT INTO schedules(id, name, cron_expr, enabled, trigger_payload, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
            """, (s['id'], s['name'], s['cron_expr'], enabled, trigger_payload, now_str()))
    return True, "OK", s['id']

def delete_schedule(sid: str):
    with db() as conn, _db_lock:
        n = conn.execute("DELETE FROM schedules WHERE id=?", (sid,)).rowcount
    return n > 0

def fire_schedule(sid: str):
    with db() as conn:
        s = conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    if not s:
        return False, "schedule 不存在", None
    s = dict(s)
    payload = json.loads(s.get('trigger_payload') or '{}')
    trigger = payload.get('trigger', 'cron')
    ok, msg = start_run(trigger=trigger)
    with db() as conn, _db_lock:
        conn.execute("UPDATE schedules SET last_run=?, last_status=? WHERE id=?",
                     (now_str(), 'fired' if ok else f'failed: {msg}', sid))
    return ok, msg, sid

# ============== Cluster（多机集群模式） ==============
import urllib.request as _urlreq
import urllib.error as _urlerr
import socket as _socket

_cluster_cache = {"peers": {}, "last_refresh": 0}
_cluster_stop = threading.Event()
_scheduler_thread = None
_cluster_thread = None

def _get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default

def _set_setting(key: str, value: str):
    with db() as conn, _db_lock:
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,datetime('now','localtime')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value)
        )

def _detect_tailscale_ip() -> str:
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().split()[0]
    except Exception:
        pass
    return ""

def get_self_id() -> str:
    sid = _get_setting("cluster.self.id")
    if sid:
        return sid
    return _socket.gethostname()

def get_self_name() -> str:
    return _get_setting("cluster.self.name") or _socket.gethostname()

def get_self_url() -> str:
    cached = _get_setting("cluster.self.url")
    if cached:
        return cached
    ip = _detect_tailscale_ip()
    if ip:
        return f"http://{ip}:{PORT}"
    return f"http://{_socket.gethostname()}:{PORT}"

def get_self_state() -> dict:
    state = get_state()
    q = get_queue_stats()
    ff, ver = ffmpeg_version()
    try:
        disk = disk_usage()
    except Exception:
        disk = {}
    return {
        "id": get_self_id(),
        "name": get_self_name(),
        "hostname": _socket.gethostname(),
        "url": get_self_url(),
        "alive": True,
        "ffmpeg": ff,
        "ffmpeg_version": ver,
        "queue": q,
        "run": state,
        "disk": disk,
        "now": now_str(),
    }

def _fetch_peer(url: str, timeout: float = 5.0):
    """GET {url}/api/cluster/state, returns dict or raises."""
    full = url.rstrip("/") + "/api/cluster/state"
    req = _urlreq.Request(full, headers={"User-Agent": "video-manager-cluster/1.0"})
    with _urlreq.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)

def _cluster_refresh_one(peer: dict) -> dict:
    """拉取单个 peer 的状态，结果存到 _cluster_cache"""
    pid = peer.get("id") or peer.get("url")
    try:
        st = _fetch_peer(peer["url"], timeout=4.0)
        _cluster_cache["peers"][pid] = {
            "id": peer.get("id") or st.get("id"),
            "name": peer.get("name") or st.get("name"),
            "url": peer["url"],
            "ok": True,
            "state": st,
            "fetched_at": now_str(),
            "latency_ms": None,
        }
    except Exception as e:
        _cluster_cache["peers"][pid] = {
            "id": peer.get("id"),
            "name": peer.get("name"),
            "url": peer["url"],
            "ok": False,
            "error": str(e),
            "fetched_at": now_str(),
        }
    return _cluster_cache["peers"][pid]

def _cluster_loop():
    while not _cluster_stop.is_set():
        try:
            _cluster_refresh_all()
        except Exception as e:
            log(f"cluster loop error: {e}", level=logging.WARNING)
        _cluster_stop.wait(30)

def _cluster_refresh_all():
    raw = _get_setting("cluster.peers", "[]")
    try:
        peers = json.loads(raw) if raw else []
    except Exception:
        peers = []
    # 清理已删除的 peer(避免 cache 里堆积幽灵)
    valid_ids = {p.get("id") or p.get("url") for p in peers}
    for cached_id in list(_cluster_cache["peers"].keys()):
        if cached_id not in valid_ids:
            del _cluster_cache["peers"][cached_id]
    for p in peers:
        if not p.get("url"):
            continue
        try:
            _cluster_refresh_one(p)
        except Exception as e:
            log(f"cluster refresh {p.get('url')}: {e}", level=logging.WARNING)
    _cluster_cache["last_refresh"] = now_str()

def start_cluster():
    global _cluster_thread
    if _cluster_thread and _cluster_thread.is_alive():
        return
    _cluster_stop.clear()
    _cluster_thread = threading.Thread(target=_cluster_loop, daemon=True, name="cluster-heartbeat")
    _cluster_thread.start()
    log("cluster heartbeat thread started")

def _cluster_aggregate_files(dir_name: str, q: str = "", sort: str = "mtime", order: str = "desc", page: int = 1, page_size: int = 0) -> dict:
    """聚合所有 peer 的文件列表(含本机)。dir_name = 'input' | 'output'
    q/sort/order/page/page_size 传给每个 peer
    """
    result = {"self": None, "peers": [], "dir": dir_name}
    # 本机
    try:
        base = INPUT_DIR if dir_name == "input" else OUTPUT_DIR
        d = list_files(base, q=q, sort=sort, order=order, page=page, page_size=page_size)
        result["self"] = {
            "id": get_self_id(),
            "name": get_self_name(),
            "url": get_self_url(),
            "files": d,
            "ok": True,
            "is_self": True,
        }
    except Exception as e:
        result["self"] = {"id": get_self_id(), "name": get_self_name(),
                          "url": get_self_url(), "ok": False, "error": str(e), "is_self": True}
    # 远端 peers
    raw = _get_setting("cluster.peers", "[]")
    try:
        peers_cfg = json.loads(raw) if raw else []
    except Exception:
        peers_cfg = []
    for p in peers_cfg:
        pid = p.get("id") or p.get("url")
        entry = {
            "id": pid,
            "name": p.get("name") or pid,
            "url": p.get("url", "").rstrip("/"),
            "ok": False,
            "files": None,
        }
        try:
            qs_parts = [f"dir={dir_name}"]
            if q:        qs_parts.append(f"q={_urlquote(q)}")
            if sort and sort != "mtime": qs_parts.append(f"sort={sort}")
            if order and order != "desc": qs_parts.append(f"order={order}")
            if page > 1:  qs_parts.append(f"page={page}")
            if page_size:  qs_parts.append(f"page_size={page_size}")
            url = f"{p['url'].rstrip('/')}/api/files/{dir_name}?" + "&".join(qs_parts)
            req = _urlreq.Request(url, headers={"User-Agent": "video-manager-cluster/1.0"})
            with _urlreq.urlopen(req, timeout=8) as r:
                body = r.read().decode("utf-8")
                data = json.loads(body)
                entry["files"] = data.get("files")
                entry["ok"] = True
        except Exception as e:
            entry["error"] = str(e)
        result["peers"].append(entry)
    return result

def update_peers(peers_list: list):
    """peers_list: [{"id":..,"name":..,"url":..}]"""
    # 校验每条
    cleaned = []
    for p in peers_list:
        if not isinstance(p, dict):
            continue
        url = (p.get("url") or "").strip().rstrip("/")
        if not url:
            continue
        if not url.startswith("http://") and not url.startswith("https://"):
            continue
        pid = (p.get("id") or p.get("name") or url).strip()
        cleaned.append({"id": pid, "name": (p.get("name") or pid).strip(), "url": url})
    _set_setting("cluster.peers", json.dumps(cleaned))
    # 立即刷新一次
    _cluster_refresh_all()
    return cleaned

def update_self(sid: str = None, sname: str = None, surl: str = None):
    if sid is not None:
        _set_setting("cluster.self.id", sid.strip())
    if sname is not None:
        _set_setting("cluster.self.name", sname.strip())
    if surl is not None:
        _set_setting("cluster.self.url", surl.strip())

# ============== main ==============
def _migrate_legacy_peers():
    """一次性迁移:从 /etc/caddy/peers.conf (旧部署) 读入 cluster.peers setting.

    后续部署不再依赖这个文件。它存在则读,不在则跳过;已经是空列表则也跳过。
    Idempotent: 重启不会重复导入(只在 DB 为空时导入)。
    """
    legacy = Path("/etc/caddy/peers.conf")
    if not legacy.exists():
        return
    raw = _get_setting("cluster.peers", "[]")
    try:
        existing = json.loads(raw) if raw else []
    except Exception:
        existing = []
    if existing:
        return  # 已迁过,跳过
    peer_re = re.compile(r"^([A-Za-z0-9_-]+)=(.+)$")
    imported = []
    try:
        with legacy.open() as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    stripped = line.lstrip("#").strip()
                else:
                    stripped = line
                m = peer_re.match(stripped)
                if not m:
                    continue
                pid, url = m.group(1).strip(), m.group(2).strip()
                if pid and url.startswith(("http://", "https://")):
                    imported.append({"id": pid, "name": pid, "url": url.rstrip("/")})
    except Exception as e:
        log(f"启动迁移读 {legacy} 失败: {e}", level=logging.WARNING)
        return
    if imported:
        update_peers(imported)
        log(f"启动迁移: 从 {legacy} 导入 {len(imported)} 个节点 -> settings:cluster.peers")


_auto_update_stop = threading.Event()
def start_auto_updater():
    """从 master (集群 master URL) 拉代码,有差异就 git reset + 重启自己。

    通过两个 DB setting 调整:
      cluster.auto_update   "1" 或 "0" (默认 1)
      cluster.master_url    master 节点的 /api/cluster/version baseurl
                            (默认用 set_self() 里的 URL)
    初始 60s 开关让其他启动 init_db 等走完,之后每 5 分钟轮询。
    """
    auto = _get_setting("cluster.auto_update", "1")
    master = _get_setting("cluster.master_url", "").strip()
    if auto != "1" or not master:
        log(f"auto-update: skip (auto={auto!r}, master={master!r})")
        return

    def _local_commit():
        try:
            return subprocess.check_output(
                ["git", "-C", str(APP_DIR), "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL, text=True, timeout=5
            ).strip()
        except Exception:
            return ""

    def _run():
        if _auto_update_stop.wait(60):
            return
        while not _auto_update_stop.wait(300):
            try:
                url = master.rstrip("/") + "/api/cluster/version"
                with _urlreq.urlopen(url, timeout=5) as r:
                    data = json.loads(r.read())
                remote = (data.get("commit") or "").strip()
                if not remote or len(remote) < 7:
                    continue
                local = _local_commit()
                if not local or local == remote:
                    continue
                log(f"auto-update: master={remote[:7]} local={local[:7]}, pulling...")
                subprocess.run(["git", "-C", str(APP_DIR), "fetch", "--depth=1", "origin", "main"],
                               check=False, timeout=60, capture_output=True)
                r = subprocess.run(["git", "-C", str(APP_DIR), "reset", "--hard", "origin/main"],
                                   check=False, timeout=30, capture_output=True, text=True)
                log(f"auto-update: git reset rc={r.returncode}")
                # schedule restart by another thread (don't block serve_forever)
                def _restart():
                    time.sleep(1.5)  # let response settle
                    subprocess.Popen(["sudo", "-n", "/usr/bin/systemctl", "restart", "video-manager"])
                threading.Thread(target=_restart, daemon=True).start()
                # 此进程即将被 systemd 重启,跳出循环
                return
            except Exception as e:
                log(f"auto-update poll error: {e}", level=logging.WARNING)

    t = threading.Thread(target=_run, daemon=True, name="auto-update")
    t.start()
    log(f"auto-update: started, polling {master} every 5 min")


def main():
    init_db()
    load_settings()
    _migrate_legacy_peers()
    start_scheduler()
    start_cluster()
    # auto-master URL: 如果用户在 settings 设了就用,否则回退到本机 self URL(自己决定不再拉)
    # 用户装了 --worker,install.sh 会在 settings 里写 cluster.master_url;没设就啥也不做。
    start_auto_updater()
    log(f"启动: 监听 {HOST}:{PORT}")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()