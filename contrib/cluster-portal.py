#!/usr/bin/env python3
"""Cluster portal — Caddy 门户背后的管理服务.

功能:
  - 读 /etc/caddy/peers.conf, 暴露 REST API 管理 NAS 节点
  - 调每个 enabled 节点的 /api/status + /api/queue/stats, 聚合成实时状态
  - 改 peers.conf 后触发 caddy reload (让新路由生效)
  - 服务动态 SPA 首页

端口: 8889 (caddy 把 / 反代过来)
"""
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request

# ---- 配置 (可用环境变量覆盖, 方便测试) ----
PEERS_CONF    = os.environ.get('PEERS_CONF', '/etc/caddy/peers.conf')
LISTEN_HOST   = os.environ.get('LISTEN_HOST', '127.0.0.1')
LISTEN_PORT   = int(os.environ.get('LISTEN_PORT') or '8889')
# 如果设置了就用 unix socket (避免 caddy 反代吃 body)
LISTEN_SOCKET = os.environ.get('LISTEN_SOCKET', '') or ''
STATUS_TIMEOUT = float(os.environ.get('STATUS_TIMEOUT', '2.0'))
HTML_FILE     = os.environ.get('HTML_FILE',
                               os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            'cluster-portal.html'))

# ---- peers.conf 读写 ----

def load_peers():
    """读 peers.conf → [{id, url, enabled}, ...]

    注释说明行 (含空格或不是 id=url 格式) 跳过; 以 # 开头的 id=url 视为禁用.
    """
    peers = []
    if not os.path.exists(PEERS_CONF):
        return peers
    import re as _re
    # 严格: id 必须是字母数字下划线连字符, url 必须是 http(s)://
    peer_re = _re.compile(r'^([A-Za-z0-9_-]+)=(.+)$')
    with open(PEERS_CONF) as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            enabled = True
            if line.startswith('#'):
                enabled = False
                stripped = line.lstrip('#').strip()
            else:
                stripped = line
            # 必须是 id=url 形式才计为 peer (注释里的中文说明行跳过)
            m = peer_re.match(stripped)
            if not m:
                continue
            id_, url = m.group(1).strip(), m.group(2).strip()
            if not id_ or not url.startswith(('http://', 'https://')):
                continue
            peers.append({'id': id_, 'url': url, 'enabled': enabled})
    return peers


def save_peers(peers):
    """写回 peers.conf. 顺序: enabled 在前, disabled (注释) 在后."""
    enabled  = [p for p in peers if p['enabled']]
    disabled = [p for p in peers if not p['enabled']]
    lines = [f"{p['id']}={p['url']}" for p in enabled]
    lines += [f"#{p['id']}={p['url']}" for p in disabled]
    # 原子写: .tmp 再 rename
    tmp = PEERS_CONF + '.tmp'
    with open(tmp, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    os.replace(tmp, PEERS_CONF)


def reload_caddy():
    """触发 caddy 重新加载. 我们用 admin off, 所以走 systemctl."""
    try:
        subprocess.run(['systemctl', 'reload', 'caddy'],
                       check=False, capture_output=True, timeout=5)
    except Exception as e:
        sys.stderr.write(f'[cluster-portal] caddy reload failed: {e}\n')

# ---- 状态聚合 ----

def fetch_nas_status(peer):
    """调 peer 的 /api/status + /api/queue/stats. 2s 超时."""
    if not peer['enabled']:
        return {'online': False, 'disabled': True}
    base = peer['url'].rstrip('/')
    out = {'online': False}
    try:
        req = urllib.request.urlopen(f'{base}/api/status', timeout=STATUS_TIMEOUT)
        st = json.loads(req.read().decode('utf-8'))
        out.update({
            'online': True,
            'running': bool(st.get('running')),
            'current_file': st.get('current_file'),
            'pid': st.get('pid'),
            'started_at': st.get('started_at'),
        })
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        out['error'] = f'status: {type(e).__name__}'
    try:
        req = urllib.request.urlopen(f'{base}/api/queue/stats', timeout=STATUS_TIMEOUT)
        q = json.loads(req.read().decode('utf-8'))
        out.update({
            'pending':      q.get('pending', 0),
            'running_count': q.get('running', 0),
            'done':         q.get('done', 0),
            'failed':       q.get('failed', 0),
            'skipped':      q.get('skipped', 0),
        })
    except Exception:
        pass
    return out

# ---- HTTP 处理器 ----

class Handler(http.server.BaseHTTPRequestHandler):
    # 强制 HTTP/1.1 + Connection: close — 不然 caddy 反代会吃掉 body (Content-Length: 0)
    # Python stdlib 默认 HTTP/1.0, 反代中间件在 HTTP/1.0 响应下处理不可靠
    protocol_version = 'HTTP/1.1'

    def handle(self):
        # 覆盖默认 handle, 主动关连接 — 避免 caddy keep-alive 边界
        self.close_connection = True
        super().handle()

    server_version = 'cluster-portal/1.0'

    def log_message(self, fmt, *args):
        # 暂时打开 debug, 看请求路径/方法/状态
        sys.stderr.write(f'[cp] {self.command} {self.path} → {fmt % args}\n')
        sys.stderr.flush()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _file(self, path, ctype='text/html; charset=utf-8'):
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404, 'not found'); return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self):
        n = int(self.headers.get('Content-Length', 0) or 0)
        if not n: return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return None

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            self._file(HTML_FILE)
        elif path == '/api/cluster/nodes':
            peers = load_peers()
            # 并发查, 总耗时 = max 而不是 sum
            results = [None] * len(peers)
            def worker(i, p):
                results[i] = fetch_nas_status(p)
            threads = [threading.Thread(target=worker, args=(i, p))
                       for i, p in enumerate(peers)]
            for t in threads: t.start()
            for t in threads: t.join(timeout=STATUS_TIMEOUT + 1)
            for i, p in enumerate(peers):
                p['status'] = results[i] or {'online': False, 'error': 'timeout'}
            self._json({'nodes': peers, 'peers_conf': PEERS_CONF})
        elif path == '/api/cluster/health':
            self._json({'ok': True, 'peers_conf': PEERS_CONF})
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split('?', 1)[0]
        if path == '/api/cluster/nodes':
            data = self._read_json()
            if data is None or not data.get('id') or not data.get('url'):
                self._json({'error': 'id 和 url 必填'}, 400); return
            node_id = re.sub(r'[^a-zA-Z0-9_-]', '', str(data['id']))
            if not node_id or node_id != data['id']:
                self._json({'error': 'id 只能含字母数字_-'}, 400); return
            url = data['url'].strip()
            if not url.startswith(('http://', 'https://')):
                self._json({'error': 'url 必须以 http:// 或 https:// 开头'}, 400); return
            peers = load_peers()
            if any(p['id'] == node_id for p in peers):
                self._json({'error': f'节点 {node_id} 已存在'}, 409); return
            peers.append({'id': node_id, 'url': url, 'enabled': True})
            save_peers(peers)
            reload_caddy()
            self._json({'ok': True, 'id': node_id})
        else:
            self.send_error(404)

    def do_PATCH(self):
        m = re.match(r'^/api/cluster/nodes/([A-Za-z0-9_-]+)$', self.path.split('?', 1)[0])
        if not m:
            self.send_error(404); return
        node_id = m.group(1)
        data = self._read_json()
        if data is None:
            self._json({'error': 'invalid json'}, 400); return
        peers = load_peers()
        for p in peers:
            if p['id'] == node_id:
                changed = False
                if 'enabled' in data:
                    p['enabled'] = bool(data['enabled']); changed = True
                if 'url' in data:
                    new_url = data['url'].strip()
                    if not new_url.startswith(('http://', 'https://')):
                        self._json({'error': 'url 必须以 http:// 或 https:// 开头'}, 400); return
                    p['url'] = new_url; changed = True
                if changed:
                    save_peers(peers)
                    reload_caddy()
                self._json({'ok': True})
                return
        self._json({'error': f'节点 {node_id} 不存在'}, 404)

    def do_DELETE(self):
        m = re.match(r'^/api/cluster/nodes/([A-Za-z0-9_-]+)$', self.path.split('?', 1)[0])
        if not m:
            self.send_error(404); return
        node_id = m.group(1)
        peers = load_peers()
        new_peers = [p for p in peers if p['id'] != node_id]
        if len(new_peers) == len(peers):
            self._json({'error': f'节点 {node_id} 不存在'}, 404); return
        save_peers(new_peers)
        reload_caddy()
        self._json({'ok': True})


def main():
    # 优先用 unix socket (避免 caddy 2.6 reverse_proxy 吃 body)
    if LISTEN_SOCKET:
        import os as _os
        import socket as _socket

        class UnixHTTPServer(http.server.ThreadingHTTPServer):
            address_family = _socket.AF_UNIX

        sock_dir = _os.path.dirname(LISTEN_SOCKET)
        if sock_dir and not _os.path.exists(sock_dir):
            _os.makedirs(sock_dir, exist_ok=True)
        if _os.path.exists(LISTEN_SOCKET):
            _os.unlink(LISTEN_SOCKET)
        httpd = UnixHTTPServer(LISTEN_SOCKET, Handler)
        # 0660 + chgrp caddy — caddy 用户能连
        try:
            import grp as _grp
            caddy_gid = _grp.getgrnam('caddy').gr_gid
            _os.chown(LISTEN_SOCKET, -1, caddy_gid)
        except (KeyError, OSError):
            pass  # 没有 caddy 组就跳过
        _os.chmod(LISTEN_SOCKET, 0o0660)
        print(f'[cluster-portal] listening on unix:{LISTEN_SOCKET}', flush=True)
        print(f'[cluster-portal] peers.conf = {PEERS_CONF}', flush=True)
        print(f'[cluster-portal] html        = {HTML_FILE}', flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\n[cluster-portal] shutting down', flush=True)
            httpd.shutdown()
        return

    httpd = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f'[cluster-portal] listening on http://{LISTEN_HOST}:{LISTEN_PORT}', flush=True)
    print(f'[cluster-portal] peers.conf = {PEERS_CONF}', flush=True)
    print(f'[cluster-portal] html        = {HTML_FILE}', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n[cluster-portal] shutting down', flush=True)
        httpd.shutdown()

if __name__ == '__main__':
    main()
