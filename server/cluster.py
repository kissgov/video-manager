# -*- coding: utf-8 -*-
"""
多机集群模式:本机身份、peer 心跳、文件聚合、auto-update。
_detect_tailscale_ip / get_self_id / get_self_name / get_self_url /
get_self_state / _fetch_peer / _cluster_refresh_one / _cluster_loop /
_cluster_refresh_all / start_cluster / _cluster_aggregate_files /
update_peers / update_self / _migrate_legacy_peers / start_auto_updater。

_cluster_cache 是 dict(原地修改,从不重新赋值),所以模块级共享没问题。
_cluster_thread / global 改为 _S.cluster_thread。
"""
import re
import json
import time
import logging
import threading
import subprocess
import socket as _socket
import urllib.request as _urlreq
from pathlib import Path

from . import config
from .config import log, now_str, APP_DIR, PORT
from .db import _get_setting, _set_setting
from .state import _S, _cluster_cache, _cluster_stop, _auto_update_stop


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
    # 延迟导入,避免 cluster <-> worker <-> queue 循环
    from .worker import get_state
    from .queue import get_queue_stats
    from .ffmpeg import ffmpeg_version
    from .files import disk_usage
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
            "_failures": 0,
            "_next_refresh": 0.0,
        }
    except Exception as e:
        prev = _cluster_cache["peers"].get(pid, {}) or {}
        failures = prev.get("_failures", 0) + 1
        # 指数退避:30s -> 60 -> 120 -> 240 -> 300(上限 5 分钟)
        backoff = min(300, 30 * (2 ** max(0, failures - 1)))
        _cluster_cache["peers"][pid] = {
            "id": peer.get("id"),
            "name": peer.get("name"),
            "url": peer["url"],
            "ok": False,
            "error": str(e),
            "fetched_at": now_str(),
            "_failures": failures,
            "_next_refresh": time.time() + backoff,
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
    now = time.time()
    for p in peers:
        if not p.get("url"):
            continue
        pid = p.get("id") or p.get("url")
        cached = _cluster_cache["peers"].get(pid)
        # 失败 peer 走指数退避,未到 _next_refresh 则跳过(保留上次状态给 UI)
        if cached and not cached.get("ok") and cached.get("_next_refresh", 0.0) > now:
            continue
        try:
            _cluster_refresh_one(p)
        except Exception as e:
            log(f"cluster refresh {p.get('url')}: {e}", level=logging.WARNING)
    _cluster_cache["last_refresh"] = now_str()

def start_cluster():
    if _S.cluster_thread and _S.cluster_thread.is_alive():
        return
    _cluster_stop.clear()
    _S.cluster_thread = threading.Thread(target=_cluster_loop, daemon=True, name="cluster-heartbeat")
    _S.cluster_thread.start()
    log("cluster heartbeat thread started")

def _cluster_aggregate_files(dir_name: str, q: str = "", sort: str = "mtime", order: str = "desc", page: int = 1, page_size: int = 0) -> dict:
    """聚合所有 peer 的文件列表(含本机)。dir_name = 'input' | 'output'
    q/sort/order/page/page_size 传给每个 peer
    """
    from .files import list_files
    from urllib.parse import quote as _urlquote
    result = {"self": None, "peers": [], "dir": dir_name}
    # 本机
    try:
        base = config.INPUT_DIR if dir_name == "input" else config.OUTPUT_DIR
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

# ============== main 一次性迁移 ==============
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
