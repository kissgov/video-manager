#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compress_video.sh 可视化管理 - 后端入口

本文件是薄入口:实际代码在 `server/` 包里。
保留这个文件是因为 systemd unit 和 install.sh 都调用 `python3 app.py`。

直接 `python3 app.py` 即可启动。Python 会把本文件所在目录加入 sys.path,
因此 `import server.app` 能找到同级的 server/ 包。
"""
from server.app import main

if __name__ == "__main__":
    main()
