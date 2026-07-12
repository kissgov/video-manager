# Video Manager

> 单文件 Python 写的视频压缩任务管理 Web UI,自带 SQLite 历史,Python 标准库零依赖。

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)

## 这是什么

给 [Ugreen DH4300Plus NAS](https://www.ugreen.com/)（RK3588）上跑的监控录像自动压缩管线配套用的管理面板：

- 实时看 worker 状态、当前处理文件、速度
- 队列管理（pending / running / done / failed / skipped）
- 实时日志跟踪（`compress.log`）
- 文件浏览器（`/input`、`/output`）
- 在线改 `compress_video.sh` 的参数（带备份）
- 管理 ofelia 定时任务 + 一键重启
- 历史压缩统计（用时、压缩比、成功率）

## 特性

- **零 pip 依赖** — `app.py` 用 `http.server` + `sqlite3`，任何 Python 3.8+ 都能跑
- **单文件后端** — `app.py` ~65KB，全部逻辑在一处
- **单 SQLite** — 历史/队列/任务都在 `data/history.db`
- **优雅检测外部运行** — UI 显示 worker 当前跑到第几个文件，不打断现成实例
- **自识别硬件加速** — RKMPP → VAAPI → V4L2 → QSV → libx264 自动降级

## 截图

_（占位 — 加一张 Web UI 截图效果立竿见影）_

## 架构

```
┌────────────────────────────────────────────────────────┐
│  Browser → http://NAS-IP:8765                          │
│                                                        │
│  ┌──────────────────────────────────────────┐          │
│  │  python3 app.py (ThreadingHTTPServer)    │          │
│  │  ├─ /api/queue       任务列表/CRUD       │          │
│  │  ├─ /api/run|stop    worker 控制         │          │
│  │  ├─ /api/logs        实时日志            │          │
│  │  ├─ /api/files       文件浏览            │          │
│  │  ├─ /api/config      脚本配置            │          │
│  │  ├─ /api/cron        ofelia 管理         │          │
│  │  └─ /static/*        SPA 前端            │          │
│  └──────────────────────────────────────────┘          │
│            │                                           │
│            ├─→ SQLite (data/history.db)                │
│            ├─→ /input → /output 软链接                 │
│            ├─→ /scripts/compress_video.sh              │
│            └─→ /scripts/compress.log                   │
│                                                        │
│  ┌──────────────────────────────────────────┐          │
│  │  ofelia-scheduler (Docker)               │          │
│  │  每天 02:00 → bash /scripts/compress_video.sh       │
│  └──────────────────────────────────────────┘          │
│            │                                           │
│            └─→ ffmpeg-worker (Docker)                  │
│                带 RKMPP/RKRGA 的 ffmpeg, libx264 软编兜底│
└────────────────────────────────────────────────────────┘
```

## 快速开始（开发机试一下）

```bash
git clone https://github.com/<your>/video-manager.git
cd video-manager

# 建软链接或编辑 app.py 顶部的 INPUT_DIR/OUTPUT_DIR
ln -s /path/to/your/camera /input
ln -s /path/to/compressed /output

python3 app.py
# → 浏览器打开 http://127.0.0.1:8765
```

## 部署到 NAS

完整步骤见 [DEPLOY.md](./DEPLOY.md)。两件事：

1. **L1 Web UI** — `app.py` + `static/` + `video-manager.service`，任何 Linux 都能跑
2. **L2 压缩管线** — `compress_video.sh` + Docker + ofelia + RKMPP 硬编（仅 RK3588）

```bash
# L1 装服务
sudo cp video-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now video-manager
```

## 关键路径

| 角色 | 路径 |
|---|---|
| Web UI | `http://NAS-IP:8765` |
| 应用目录 | `/volume1/scripts/video-manager/` |
| 数据库 | `data/history.db`（gitignored） |
| 日志 | `logs/app.log` / `logs/stdout.log` |
| 输入 | `/input` → 摄像头录像目录 |
| 输出 | `/output` → 压缩后目录 |
| 压缩脚本 | `/scripts/compress_video.sh` |

## 安全

- 当前**无认证**，仅适合内网/受信任网络
- `video-manager.service` 已开 `NoNewPrivileges=true` + `ProtectSystem=full`
- 外网暴露前请套反代 + BasicAuth，或前置 VPN

## 路线图

- [ ] 鉴权（BasicAuth + 反代）
- [ ] WebSocket 推送替代轮询
- [ ] 失败任务批量重试（已支持单个，重试 API 现成）
- [ ] Prometheus `/metrics`
- [ ] 迁移到 FastAPI（保持单文件分发）

## 贡献

PR 欢迎。建议在改 `app.py` 前先跑 `python3 -m py_compile app.py`，CI 还没接。

## 协议

[MIT](./LICENSE)

## 致谢

- [Ugreen](https://www.ugreen.com/) — DH4300Plus 硬件平台
- [Rockchip](https://www.rock-chips.com/) — MPP/RKRGA
- [ofelia](https://github.com/mcuadros/ofelia) — Docker 内 cron
- ffmpeg 项目和所有 encoder maintainer
