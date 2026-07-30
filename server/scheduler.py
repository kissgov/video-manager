# -*- coding: utf-8 -*-
"""
UI 定时调度器(schedules 表 + cron 解析 + 后台 tick)。
cron_next_run / _parse_cron_field / _next_run_time / _scheduler_tick /
_scheduler_loop / start_scheduler / list_schedules / upsert_schedule /
delete_schedule / fire_schedule。

所有原 `global _scheduler_thread` 标量改为 `_S.scheduler_thread`(见 server/state.py)。
"""
import json
import logging
import threading
from datetime import datetime, timedelta

from .config import log, now_str
from .db import db, _db_lock
from .state import _S, _scheduler_stop


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
    # 延迟导入,避免 scheduler <-> worker 循环(worker 不依赖 scheduler,但保持对称)
    from .worker import start_run
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
    if _S.scheduler_thread and _S.scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _S.scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    _S.scheduler_thread.start()
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
    from .worker import start_run
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
