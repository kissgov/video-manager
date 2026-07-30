# -*- coding: utf-8 -*-
"""
配置 / 常量 / 日志 / 通用工具。
集中原 app.py 顶部的路径常量、编码参数、logging handler、以及 log/now_str/json_response/read_text 工具。
"""
import os
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path

# ============== 配置 ==============
# 默认装在脚本同级目录(env VIDEO_MANAGER_DIR 可覆盖)。这样 install.sh
# 装到 /opt/video-manager 或 /home/x/scripts/video-manager 都能用。
# 注意:本模块位于 server/ 子目录,parent.parent 才是项目根(原单文件用 parent)。
APP_DIR        = Path(os.environ.get("VIDEO_MANAGER_DIR", str(Path(__file__).resolve().parent.parent)))
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
# 运行时可被 load_settings/update_settings 重新赋值 -> 其它模块一律用 config.INPUT_DIR 属性访问
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

def _make_rotating_handler(path, max_bytes, backup_count, fmt, level):
    """构造 RotatingFileHandler,容错:父目录不存在则建;仍失败则退化为 MemoryHandler-free 的 NullHandler,
    保证 import 不会因日志路径不可写而崩(新装机/沙箱里 /home/kxrdyf/scripts 可能不存在)。"""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(
            str(p), maxBytes=max_bytes, backupCount=backup_count,
            encoding="utf-8", delay=True,
        )
    except Exception:
        h = logging.NullHandler()
    h.setLevel(level)
    h.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
    return h

_app_handler = _make_rotating_handler(
    APP_LOG_PATH, LOG_MAX_BYTES, LOG_BACKUP_APP,
    "%(asctime)s [%(levelname)-7s] %(module)s.%(funcName)s:%(lineno)d  %(message)s",
    LOG_LEVEL_FILE,
)

_compress_handler = _make_rotating_handler(
    SCRIPT_LOG, LOG_MAX_BYTES, LOG_BACKUP_COMPACT,
    "[%(asctime)s] %(message)s",
    LOG_LEVEL_COMPACT,
)

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

# ============== 编码参数 (从 compress_video.sh 同步) ==============
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

_LOCK_FILE      = Path(os.environ.get("VIDEO_MANAGER_LOCK_FILE",
                                    str(APP_DIR / "compress.lock")))

# ============== 脚本配置区允许修改的 key ==============
# 在 compress_video.sh 里,我们只允许改 "配置区" 的几个变量
CONFIG_KEYS = [
    "OUTPUT_WIDTH", "OUTPUT_HEIGHT", "OUTPUT_FPS",
    "SOFT_CODEC", "SOFT_PRESET", "SOFT_CRF",
    "VAAPI_QP", "NICE_LEVEL", "MAX_LOG_LINES", "MIN_FILE_SIZE",
]

# ============== 任务队列(tasks 表) ==============
QUEUE_STATUSES = ("pending", "running", "done", "failed", "skipped")

# 允许排序的列(防 SQL 注入)
_SORTABLE_COLS = {
    "id", "rel_path", "size", "output_size",
    "attempts", "status", "ended_at",
    "duration_sec", "ratio",   # 表达式列,在 SELECT 里定义
}

# 缩略图缓存目录
THUMB_DIR = APP_DIR / "data" / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
PB_THUMB_DIR = APP_DIR / "data" / "pb_thumbs"
PB_THUMB_DIR.mkdir(parents=True, exist_ok=True)

# ============== Settings 路径校验:禁止选择的路径前缀 ==============
# (系统 / 挂载点目录，容易误选)
_BAD_PATH_PREFIXES = (
    "/proc", "/sys", "/dev", "/run", "/boot", "/etc", "/var", "/usr",
    "/lib", "/lib64", "/bin", "/sbin", "/opt", "/srv", "/mnt", "/media",
    "/tmp", "/root", "/home",
    str(APP_DIR),                  # 不能把输出指到 app 自身目录里
)
