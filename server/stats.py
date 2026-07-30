# -*- coding: utf-8 -*-
"""
统计 / 历史。
get_stats / get_history。
"""
from .db import db, _db_lock


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
