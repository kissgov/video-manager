# -*- coding: utf-8 -*-
"""
HTTP 路由 Handler。
原 app.py 里的 `class Handler(BaseHTTPRequestHandler)` 整体搬过来。

注意:INPUT_DIR / OUTPUT_DIR 在运行时会被 db.update_settings 重新赋值
(config.INPUT_DIR = ...),所以这里必须用 `config.INPUT_DIR` 属性访问,
不能用 `from .config import INPUT_DIR`(那样会拿到 import 时的快照)。
其它不可变常量直接 import 没问题。

保留原文件里的两处潜在 bug(不动行为):
  - do_GET /api/files/info 里 `fp = _safe_join(...)` 缺 `self.`(原文件就这样)
  - do_POST /api/cluster/files 里用了未定义的 `qs`(原文件就这样)
"""
import os
import re
import json
import time
import logging
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote as _urlquote
from http.server import BaseHTTPRequestHandler
import urllib.request as _urlreq

from . import config
from .config import (
    log, json_response,
    SCRIPT_LOG, SCRIPT_LOCK, SCRIPT_PATH, STATIC_DIR, OFELIA_INI, APP_DIR,
    CONFIG_KEYS, _INPUT_DIR_DEFAULT, _OUTPUT_DIR_DEFAULT, PB_THUMB_DIR,
)
from .db import _get_setting, _set_setting, update_settings
from .worker import get_state, update_current_file, read_log_tail, start_run, stop_run
from .files import (
    list_files, get_file_dates, get_or_make_thumbnail, get_or_make_thumbs, disk_usage,
)
from .stats import get_stats, get_history
from .ffmpeg import (
    ffmpeg_version, detect_hwaccel_hint,
    _get_rkmpp_qp, _get_rkmpp_bitrate_cap, _get_force_recompress,
)
from .ofelia import (
    read_script_config, update_script_config,
    read_ofelia_jobs, update_ofelia_jobs, restart_ofelia,
)
from .scheduler import (
    cron_next_run, _next_run_time,
    list_schedules, upsert_schedule, delete_schedule, fire_schedule,
)
from .queue import (
    sync_tasks_from_input, get_queue_stats, list_tasks,
    retry_tasks, backfill_task_durations, delete_tasks,
)
from .cluster import (
    get_self_state, get_self_id, get_self_name, get_self_url,
    _cluster_cache, _cluster_refresh_all, _cluster_aggregate_files,
    update_peers, update_self,
)


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
            base = config.INPUT_DIR
        elif dir_param == "output":
            base = config.OUTPUT_DIR
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
            base = config.INPUT_DIR
        elif dir_param == "output":
            base = config.OUTPUT_DIR
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

    def _peer_url(self, peer_id: str):
        """返回 peer 的 base url(已 rstrip /),不存在/未配置返回 None。"""
        if not peer_id:
            return None
        try:
            raw = _get_setting("cluster.peers", "[]")
            peers = json.loads(raw) if raw else []
        except Exception:
            peers = []
        peer = next((p for p in peers if p.get("id") == peer_id), None)
        if not peer:
            return None
        return peer.get("url", "").rstrip("/") or None

    def _proxy_peer_image(self, target_url: str):
        """通用:从 peer 拉一张图片(jpeg/png)并流式透传。"""
        req = _urlreq.Request(target_url, headers={"User-Agent": "video-manager-proxy/1.0"})
        try:
            upstream = _urlreq.urlopen(req, timeout=15)
        except Exception as e:
            return json_response(self, 502, {"ok": False, "error": f"上游 peer 不可达: {e}"})
        status = upstream.status
        passthrough = {"Content-Type", "Content-Length", "Cache-Control"}
        self.send_response(status)
        for hname, v in upstream.getheaders():
            if hname in passthrough:
                self.send_header(hname, v)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"proxy image error: {e}", level=logging.WARNING)
        return None

    def _proxy_peer_thumb(self, qs):
        """代理远端 peer 的单张缩略图。?peer=ID&dir=input|output&path=xxx"""
        peer_id = qs.get("peer", [""])[0]
        dir_param = qs.get("dir", ["output"])[0]
        file_param = qs.get("path", [""])[0]
        if not file_param:
            return json_response(self, 400, {"ok": False, "error": "需要 path 参数"})
        base = self._peer_url(peer_id)
        if not base:
            return json_response(self, 404, {"ok": False, "error": f"peer {peer_id!r} 不存在"})
        target_url = f"{base}/api/files/thumb?dir={dir_param}&path={_urlquote(file_param)}"
        return self._proxy_peer_image(target_url)

    def _proxy_peer_pb_thumbs(self, qs):
        """代理远端 peer 的多帧缩略图元数据,重写 url 指向本地代理。
        ?peer=ID&dir=input|output&path=xxx&count=24
        上游返回 {duration, thumbs:[{i,t,url,cached}]},url 形如
        /api/pb/thumb?dir=X&h=Y&i=Z,改写为 /api/cluster/pb/thumb?peer=ID&dir=X&h=Y&i=Z
        """
        peer_id = qs.get("peer", [""])[0]
        dir_param = qs.get("dir", ["output"])[0]
        file_param = qs.get("path", [""])[0]
        count = qs.get("count", ["20"])[0]
        if not file_param:
            return json_response(self, 400, {"ok": False, "error": "需要 path 参数"})
        base = self._peer_url(peer_id)
        if not base:
            return json_response(self, 404, {"ok": False, "error": f"peer {peer_id!r} 不存在"})
        target_url = (f"{base}/api/pb/thumbs?dir={dir_param}"
                      f"&path={_urlquote(file_param)}&count={count}")
        req = _urlreq.Request(target_url, headers={"User-Agent": "video-manager-proxy/1.0"})
        try:
            with _urlreq.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return json_response(self, 502, {"ok": False, "error": f"上游 peer 不可达: {e}"})
        # 重写每张 thumb 的 url,改走本地 /api/cluster/pb/thumb 代理
        for th in data.get("thumbs", []) or []:
            orig = th.get("url", "")
            h_val = ""
            i_val = "0"
            if orig:
                oq = parse_qs(urlparse(orig).query)
                h_val = oq.get("h", [""])[0]
                i_val = oq.get("i", ["0"])[0]
            th["url"] = (f"/api/cluster/pb/thumb?peer={_urlquote(peer_id)}"
                         f"&dir={dir_param}&h={h_val}&i={i_val}")
        return json_response(self, 200, data)

    def _proxy_peer_pb_thumb(self, qs):
        """代理远端 peer 的单张进度条缩略图。?peer=ID&dir=X&h=Y&i=Z"""
        peer_id = qs.get("peer", [""])[0]
        dir_param = qs.get("dir", ["output"])[0]
        h = qs.get("h", [""])[0]
        i = qs.get("i", ["0"])[0]
        base = self._peer_url(peer_id)
        if not base:
            return json_response(self, 404, {"ok": False, "error": f"peer {peer_id!r} 不存在"})
        target_url = f"{base}/api/pb/thumb?dir={dir_param}&h={h}&i={i}"
        return self._proxy_peer_image(target_url)

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

    def _guess_mime(self, name: str) -> str:
        """根据扩展名猜 Content-Type。"""
        n = name.lower()
        if n.endswith(".html") or n.endswith(".htm"): return "text/html; charset=utf-8"
        if n.endswith(".js") or n.endswith(".mjs"):  return "application/javascript; charset=utf-8"
        if n.endswith(".css"):  return "text/css; charset=utf-8"
        if n.endswith(".json"): return "application/json; charset=utf-8"
        if n.endswith(".png"):  return "image/png"
        if n.endswith(".svg"):  return "image/svg+xml"
        if n.endswith((".jpg", ".jpeg")): return "image/jpeg"
        if n.endswith(".gif"):  return "image/gif"
        if n.endswith(".webp"): return "image/webp"
        if n.endswith(".ico"):  return "image/x-icon"
        if n.endswith(".woff"): return "font/woff"
        if n.endswith(".woff2"): return "font/woff2"
        if n.endswith(".ttf"):  return "font/ttf"
        if n.endswith(".map"):  return "application/json; charset=utf-8"
        if n.endswith(".txt"):  return "text/plain; charset=utf-8"
        return "application/octet-stream"

    def _serve_static(self, rel: str):
        """服务 React 构建产物 (static/dist/)。

        - rel 为空或 "index.html" → 主入口 index.html (Cache-Control: no-store)
        - rel 在 web root 下命中真实文件 → 返回文件 (带扩展名资用长缓存,HTML 不缓存)
        - 看起来像路由(无扩展名)且文件不存在 → SPA fallback 回 index.html
        - 看起来像文件(有扩展名)且文件不存在 → 404,避免掩盖真实问题

        防穿越: 拒绝反斜杠 / 空字节 / ".." 段;resolve 后必须仍在 WEB_ROOT 内。
        """
        web_root = (STATIC_DIR / "dist").resolve()
        # 防穿越
        if "\\" in rel or "\x00" in rel or any(seg == ".." for seg in rel.split("/")):
            return self.send_error(403)
        try:
            full = (web_root / rel).resolve() if rel else web_root
        except Exception:
            return self.send_error(403)
        if not (str(full) == str(web_root) or str(full).startswith(str(web_root) + os.sep)):
            return self.send_error(403)

        # 命中文件
        if full.is_file():
            is_html = full.name.lower() == "index.html"
            cache = "no-store" if is_html else "public, max-age=86400"
            try:
                data = full.read_bytes()
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                return None
            except Exception as e:
                log(f"static read error {full}: {e}", level=logging.WARNING)
                return self.send_error(500, str(e))
            self.send_response(200)
            self.send_header("Content-Type", self._guess_mime(full.name))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return None

        # 文件不存在 → 看是否需要 SPA fallback
        last_segment = rel.rsplit("/", 1)[-1] if rel else ""
        if "." in last_segment:
            # 有扩展名但文件不存在 → 真 404
            return self.send_error(404)
        # 无扩展名 → 视为客户端路由,回 index.html (HashRouter 不需要但保留兼容)
        try:
            idx = (web_root / "index.html").read_bytes()
        except FileNotFoundError:
            return self.send_error(404, "index.html not found")
        except Exception as e:
            log(f"index.html read error: {e}", level=logging.WARNING)
            return self.send_error(500, str(e))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(idx)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(idx)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    def _sse_logs(self, qs):
        """SSE 推送:增量发送 compress.log 新行。
        ?since=N  仅推送行号 > N 的(默认从头补一次尾部)
        ?level=... 可选过滤(服务端做,减少带宽)
        连接最长 10 分钟,之后客户端自动重连(避免长连接堆积)。
        """
        try:
            since = int(qs.get("since", ["0"])[0])
        except ValueError:
            since = 0
        level = (qs.get("level", ["all"])[0] or "all")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # nginx 反代时禁缓冲
        self.end_headers()
        last = since
        deadline = time.time() + 600  # 10 min 上限
        last_heartbeat = time.time()
        try:
            # 初始心跳
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while time.time() < deadline:
                result, total = read_log_tail(
                    limit=500, since=last, level=level, search=None, max_lines=500,
                )
                for line_no, text in result:
                    payload = json.dumps({"n": line_no, "text": text, "total": total},
                                         ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                if result:
                    last = result[-1][0]
                    self.wfile.flush()
                    last_heartbeat = time.time()
                # 心跳:空闲时每 15s 发一次注释行,防止代理/浏览器因空闲超时断连
                if time.time() - last_heartbeat >= 15:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.time()
                time.sleep(1.0)
            # 优雅关闭,让客户端重连
            self.wfile.write(b"event: end\ndata: timeout\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 客户端关了连接
            pass

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

        # 静态: 入口 / 和 /index.html (其它静态资源 + SPA fallback 在 do_GET 末尾兜底)
        if path == "/" or path == "/index.html":
            return self._serve_static("index.html")

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

        if path == "/api/logs/stream":
            # SSE: 实时推送新日志行,取代前端轮询 /api/logs
            return self._sse_logs(qs)

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
                config.INPUT_DIR,
                q=qs.get("q", [""])[0],
                sort=qs.get("sort", ["mtime"])[0],
                order=qs.get("order", ["desc"])[0],
                page=int(qs.get("page", ["1"])[0]),
                page_size=int(qs.get("page_size", ["0"])[0]),
            )})
        if path == "/api/files/output":
            return json_response(self, 200, {"files": list_files(
                config.OUTPUT_DIR,
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
            base = config.INPUT_DIR if dir_param == "input" else config.OUTPUT_DIR
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
            base = config.INPUT_DIR if dir_param == "input" else config.OUTPUT_DIR
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
                "input_dir":  str(config.INPUT_DIR),
                "output_dir": str(config.OUTPUT_DIR),
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
                "input_dir":  str(config.INPUT_DIR),
                "output_dir": str(config.OUTPUT_DIR),
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

        # ---- 代理:远端 peer 的缩略图(列表缩略图 + 进度条缩略图)----
        if path == "/api/cluster/thumb":
            return self._proxy_peer_thumb(qs)
        if path == "/api/cluster/pb/thumbs":
            return self._proxy_peer_pb_thumbs(qs)
        if path == "/api/cluster/pb/thumb":
            return self._proxy_peer_pb_thumb(qs)

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

        # ---- 静态资源兜底 + SPA fallback ----
        # 所有非 /api/ 路径都交给 _serve_static:命中文件就吐文件,无扩展名的视为
        # 客户端路由回 index.html,有扩展名但文件不存在返回 404。/api/ 路径未匹配走 404。
        if not path.startswith("/api/"):
            rel = path.lstrip("/")
            return self._serve_static(rel)

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
            base = config.INPUT_DIR if dir_param == "input" else config.OUTPUT_DIR
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
