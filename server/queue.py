# -*- coding: utf-8 -*-
"""
任务队列(tasks 表)。
sync_tasks_from_input / get_queue_stats / list_tasks / retry_tasks /
backfill_task_durations / delete_tasks。
"""
from datetime import datetime as _dt

from . import config
from .config import log, now_str, QUEUE_STATUSES, _SORTABLE_COLS
from .db import db, _db_lock
from .files import _walk_mp4


def sync_tasks_from_input() -> dict:
    """从 /input 和 /output 扫描,同步 tasks 表。返回本次新增/更新的统计。"""
    added_input = added_done = updated_done = reconciled = 0
    # 先一轮走,把 /input 和 /output 里的 rel_path 收齐
    input_seen: set[str]  = set()
    output_seen: set[str] = set()
    for rel, _full, size in _walk_mp4(config.INPUT_DIR):
        input_seen.add(rel)
    for rel, _full, size in _walk_mp4(config.OUTPUT_DIR):
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
                    size = (config.INPUT_DIR / rel).stat().st_size
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
            output_file = config.OUTPUT_DIR / row["rel_path"]
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
        out = config.OUTPUT_DIR / rel
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
