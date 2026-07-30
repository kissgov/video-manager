# -*- coding: utf-8 -*-
"""
跨模块共享的可变全局状态。

设计要点(见任务约束 #2):
  - 锁 / Event / dict 这些"对象本身不会被重新赋值、只调用方法或原地修改"的,
    直接作为本模块的模块级变量,其它模块 `from .state import _db_lock` 拿到的是
    同一个对象引用,跨模块共享没问题。
  - 那些会被"重新赋值"的标量(_worker_thread / _run_id / _current_file / ...),
    挂在 `_S` 对象上,所有读写一律走 `_S.xxx`。这样避免了
    `from .state import _worker_thread` 后再 `_worker_thread = ...` 只改本地绑定、
    不影响其它模块的经典陷阱。
"""
import threading


class _State:
    pass


_S = _State()

# ---- 会被重新赋值的标量(原 app.py 里的 global _xxx) ----
_S.worker_thread   = None     # Thread 对象(取代旧的 _proc = Popen) / 原 _worker_thread
_S.run_id          = None     # 当前 runs.id / 原 _run_id
_S.current_file    = None     # 解析自 log 的 "正在处理" 文件 / 原 _current_file
_S.started_at      = None     # 原 _started_at
_S.ext_cache       = None     # detect_external_job 的缓存 / 原 _ext_cache
_S.ext_cache_ts    = 0        # 原 _ext_cache_ts
_S.ffmpeg_proc     = None     # 当前 ffmpeg 子进程(用于 stop) / 原 _ffmpeg_proc
_S.log_pos         = 0        # 原 _log_pos(原文件定义后未实际读写,保留)
_S.scheduler_thread = None    # 原 _scheduler_thread
_S.cluster_thread   = None    # 原 _cluster_thread

# ---- 锁(只 acquire/release,从不重新赋值 -> 模块级即可) ----
_db_lock          = threading.Lock()
_state_lock       = threading.Lock()
_log_pos_lock     = threading.Lock()
_current_file_lock = threading.Lock()
_config_lock      = threading.Lock()
_ofelia_lock      = threading.Lock()
_settings_lock    = threading.Lock()

# ---- Event(只 set/clear/wait/is_set,从不重新赋值 -> 模块级即可) ----
_stop_event       = threading.Event()    # worker 停止信号 / 原 _stop_event
_scheduler_stop   = threading.Event()    # 原 _scheduler_stop
_cluster_stop     = threading.Event()    # 原 _cluster_stop
_auto_update_stop = threading.Event()    # 原 _auto_update_stop

# ---- dict(原地修改,从不重新赋值 -> 模块级即可) ----
_cluster_cache = {"peers": {}, "last_refresh": 0}   # 原 _cluster_cache
_config_cache  = {"text": None, "mtime": 0}          # 原 _config_cache(原文件定义后未实际使用,保留)
