# -*- coding: utf-8 -*-
"""
数据库 + settings 读写 helper。
原 app.py 里的 db()/init_db() 以及 _settings_table/_read_setting/
load_settings/update_settings/_validate_path/_get_setting/_set_setting/
_get_setting_int/_get_setting_bool 全部集中到这里。
"""
import os
import logging
import sqlite3
import threading
from pathlib import Path

from . import config
from .config import log, DATA_DIR, DB_PATH
from .state import _db_lock, _settings_lock

# ============== 数据库 ==============
# 线程本地连接复用:原 db() 每次 new 一个 sqlite3.connect,而 `with db() as conn`
# 只 commit 不 close,会泄漏 fd。改成每线程一个长连接,`with` 仍走 sqlite3 的
# __exit__(commit/rollback),连接常驻线程本地,不重复 open/close。
# 配合 _db_lock 串行化写,check_same_thread=False + 线程本地 => 无跨线程并发使用。
_tl = threading.local()

def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = getattr(_tl, "conn", None)
    if conn is not None:
        return conn
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # 启用 WAL:读不阻塞写,多线程下吞吐更好(若失败忽略,只读 fs 也能跑)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    _tl.conn = conn
    return conn

def close_thread_conn():
    """关闭当前线程的连接(主要用于测试/清理)。"""
    conn = getattr(_tl, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _tl.conn = None

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
        # 补充索引:历史详情/集群回填/队列按状态分页都走这些
        conn.execute("CREATE INDEX IF NOT EXISTS idx_run_files_run_id ON run_files(run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_last_run_id ON tasks(last_run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at)")
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

# ============== Settings（路径配置，热加载） ==============
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
    with _settings_lock:
        config.INPUT_DIR  = _read_setting("input_dir",  config._INPUT_DIR_DEFAULT)
        config.OUTPUT_DIR = _read_setting("output_dir", config._OUTPUT_DIR_DEFAULT)
        try:
            with _settings_table() as conn:
                conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                             ("input_dir", str(config.INPUT_DIR)))
                conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                             ("output_dir", str(config.OUTPUT_DIR)))
        except Exception as e:
            log(f"settings 默认值写库失败: {e}", level=logging.WARNING)
    log(f"加载路径: input={config.INPUT_DIR}  output={config.OUTPUT_DIR}")

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
    for bp in config._BAD_PATH_PREFIXES:
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
    new_in_raw  = str(updates.get("input_dir",  str(config.INPUT_DIR))).strip()
    new_out_raw = str(updates.get("output_dir", str(config.OUTPUT_DIR))).strip()
    ok1, m1, p1 = _validate_path(new_in_raw,  must_be_writable=False, must_be_readable=True)
    if not ok1:
        return False, f"input_dir: {m1}", None
    ok2, m2, p2 = _validate_path(new_out_raw, must_be_writable=True,  must_be_readable=True)
    if not ok2:
        return False, f"output_dir: {m2}", None
    if p1 == p2:
        return False, "输入和输出不能是同一目录", None
    in_changed  = p1 != config.INPUT_DIR
    out_changed = p2 != config.OUTPUT_DIR
    with _settings_lock:
        with _settings_table() as conn:
            for k, v in [("input_dir", str(p1)), ("output_dir", str(p2))]:
                conn.execute(
                    "INSERT INTO settings(key,value,updated_at) VALUES(?,?,datetime('now','localtime')) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (k, v)
                )
        config.INPUT_DIR  = p1
        config.OUTPUT_DIR = p2
    log(f"路径设置更新: input={config.INPUT_DIR}  output={config.OUTPUT_DIR}")
    # 自动重新扫描新输入目录
    try:
        from .queue import sync_tasks_from_input   # 延迟导入,避免 db <-> queue 循环
        sync_tasks_from_input()
        log("已重新扫描输入目录,新文件加入队列")
    except Exception as e:
        log(f"sync_tasks_from_input 失败: {e}", level=logging.WARNING)
    return True, "OK", {
        "input_dir":     str(config.INPUT_DIR),
        "output_dir":    str(config.OUTPUT_DIR),
        "input_changed": in_changed,
        "output_changed": out_changed,
    }

# ============== settings 通用读写(原 _get_setting / _set_setting) ==============
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
