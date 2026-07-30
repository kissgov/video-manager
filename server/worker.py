# -*- coding: utf-8 -*-
"""
进程管理 / Worker。
proc_alive / detect_external_job / etime_to_start / get_state /
update_current_file / read_log_tail / _run_loop / start_run /
_watch_run / invalidate_ext_cache / get_descendants / _kill_tree /
stop_run / _pid_alive / _rotate_log / _set_task_status。

所有原 `global _xxx` 标量改为 `_S.xxx` (见 server/state.py)。
"""
import os
import re
import time
import signal
import logging
import threading
import subprocess
import fcntl as _fcntl
from datetime import datetime, timedelta

from . import config
from .config import (
    log, now_str,
    INPUT_DIR as _INPUT_DIR_PLACEHOLDER,  # 仅占位,实际用 config.INPUT_DIR
    _LOCK_FILE, SCRIPT_LOG,
    _OUTPUT_HEIGHT, _OUTPUT_WIDTH, _OUTPUT_FPS, _MIN_FILE_SIZE,
)
from .state import _state_lock, _stop_event, _current_file_lock, _S
from .db import db, _db_lock
from .ffmpeg import _resolve_ffmpeg_bin, _detect_hwaccel, _run_ffmpeg, _get_force_recompress
from .files import human_size
from .queue import sync_tasks_from_input


def proc_alive():
    return _S.worker_thread is not None and _S.worker_thread.is_alive()

# 检测系统中是否有"看起来像压缩任务"的进程在跑
# 返回 dict 或 None
def detect_external_job():
    """返回 {pid, script_pid, current_file, started_at} 或 None。"""
    now = time.time()
    if _S.ext_cache and now - _S.ext_cache_ts < 1.5:
        return _S.ext_cache
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
            _S.ext_cache = {
                "pid":          ffmpeg_pid or script_pid,
                "script_pid":   script_pid,
                "current_file": cur_file,
                "started_at":   started_at,
                "external":     True,
            }
        else:
            _S.ext_cache = None
        _S.ext_cache_ts = now
        return _S.ext_cache
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
                "run_id":       _S.run_id,
                "current_file": _S.current_file,
                "started_at":   _S.started_at,
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
def update_current_file():
    """从 log 解析当前正在压缩的文件,写入全局状态。"""
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
        _S.current_file = cur

# ============== Worker (Python 取代 compress_video.sh) ==============
def _rotate_log():
    """已废弃:_compress_handler (RotatingFileHandler) 自动按 size 轮转。
    保留为空函数以兼容 worker 末尾的旧调用点。"""
    pass

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
    log(f"========================================")
    log(f"压缩任务启动 (Python worker v1, run_id={run_id}, trigger={trigger})")
    ffmpeg_bin = _resolve_ffmpeg_bin()
    hwaccel = _detect_hwaccel()
    log(f"输入: {config.INPUT_DIR}  输出: {config.OUTPUT_DIR}")
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
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
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
            input_file  = config.INPUT_DIR / rel
            output_file = config.OUTPUT_DIR / rel

            _set_task_status(tid, "running",
                             started_at=now_str(),
                             attempts=None,  # 用 SQL 自增在下面处理
                             last_run_id=run_id)
            # attempts 自增
            with db() as conn, _db_lock:
                conn.execute("UPDATE tasks SET attempts = attempts + 1 WHERE id=?", (tid,))
                conn.commit()

            with _state_lock:
                _S.current_file = rel

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
            _S.current_file = None
        log(f"worker 退出")

def start_run(trigger="manual"):
    if proc_alive():
        return False, "本服务启动的任务正在运行"
    ext = detect_external_job()
    if ext:
        return False, f"检测到外部已有压缩任务在运行(script_pid={ext['script_pid']}),请先停止或等其完成"
    invalidate_ext_cache()
    try:
        _S.started_at = now_str()
        _S.current_file = None
        _stop_event.clear()
        # 先写 runs 表拿 run_id
        with db() as conn, _db_lock:
            cur = conn.execute(
                "INSERT INTO runs(started_at, trigger) VALUES(?, ?)",
                (_S.started_at, trigger)
            )
            _S.run_id = cur.lastrowid
        _S.worker_thread = threading.Thread(
            target=_run_loop, args=(_S.run_id, trigger), daemon=True
        )
        _S.worker_thread.start()
        log(f"启动任务 thread run_id={_S.run_id} trigger={trigger}")
        return True, f"已启动 run_id={_S.run_id}"
    except Exception as e:
        _S.worker_thread = None
        return False, f"启动失败: {e}"

def _watch_run(run_id):
    """适配旧名:不再使用。新逻辑都在 _run_loop 里。"""
    pass

def invalidate_ext_cache():
    _S.ext_cache = None
    _S.ext_cache_ts = 0

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
    invalidate_ext_cache()
    if proc_alive():
        # 告诉 worker 停止(下一个文件之前检查 stop_event)
        _stop_event.set()
        # 同时 SIGTERM 当前 ffmpeg 子进程(如果有),2 秒后 SIGKILL
        proc = None
        with _state_lock:
            proc = _S.ffmpeg_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
        log("已发送停止信号")
        # 等 worker 退出(最多 10s)
        try:
            if _S.worker_thread:
                _S.worker_thread.join(timeout=10)
        except Exception:
            pass
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGKILL)
            except ProcessLookupError:
                pass
        with _state_lock:
            _S.worker_thread = None
            _S.ffmpeg_proc   = None
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
