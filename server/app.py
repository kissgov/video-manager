# -*- coding: utf-8 -*-
"""
应用入口:main()。
原 app.py 末尾的 main() 搬过来。被根目录 app.py 薄入口调用。
"""
from http.server import ThreadingHTTPServer

from .config import log, HOST, PORT
from .db import init_db, load_settings
from .scheduler import start_scheduler
from .cluster import _migrate_legacy_peers, start_cluster, start_auto_updater
from .handler import Handler


def main():
    init_db()
    load_settings()
    _migrate_legacy_peers()
    start_scheduler()
    start_cluster()
    # auto-master URL: 如果用户在 settings 设了就用,否则回退到本机 self URL(自己决定不再拉)
    # 用户装了 --worker,install.sh 会在 settings 里写 cluster.master_url;没设就啥也不做。
    start_auto_updater()
    log(f"启动: 监听 {HOST}:{PORT}")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
