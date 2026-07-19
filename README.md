# Video Manager

> 单文件 Python 写的视频压缩任务管理 Web UI，自带 SQLite 历史，Python 标准库零依赖。  
> 支持多机集群部署（一个 NAS 一个节点，统一 web UI 聚合）。

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)

---

## 5 秒上手（全新 NAS 一键部署）

```bash
git clone https://github.com/kissgov/video-manager.git
cd video-manager
bash contrib/install.sh
```

跑完会输出：

```
✅ 安装完成!
  本机 LAN:   http://192.168.2.104:8765/
  常用命令:    systemctl status video-manager
```

打开浏览器 → 完成。

---

## 加新节点

在第二台 NAS 上：

```bash
git clone https://github.com/kissgov/video-manager.git
cd video-manager
bash contrib/install.sh --worker --join=http://100.x.x.x:8765
```

回到第一台 NAS → 集群 tab → 看到新节点了。

---

## 常用命令

| 场景 | 命令 |
|---|---|
| 安装主节点 | `bash contrib/install.sh` |
| 安装 worker | `bash contrib/install.sh --worker --join=URL` |
| 一同开公网 HTTPS | 加 `--funnel`（需 Tailscale 启用 Funnel） |
| 升级到最新版 | `bash contrib/install.sh --update` |
| 卸载（保留数据） | `bash contrib/install.sh --uninstall` |
| 看状态 | `systemctl status video-manager` |
| 看日志 | `journalctl -u video-manager -f` |
| 重启服务 | UI 上有按钮，或 `sudo systemctl restart video-manager` |

---

## 第一次安装时给 sudo 免密

`install.sh` 第一次跑时如果发现 sudo 提示要密码，会自动停下来教你怎么破：

```bash
sudo bash contrib/install-sudoers.sh   # 一次,以后免密
```

之后 install.sh 不会再问密码。

---

## 这是什么

给 [Ugreen DH4300Plus NAS](https://www.ugreen.com/)（RK3588）上跑的监控录像自动压缩管线配套用的管理面板：

- 实时看 worker 状态、当前处理文件、速度
- 队列管理（pending / running / done / failed / skipped）
- 实时日志跟踪
- 文件浏览器（输入/输出）
- 在线改压缩脚本参数（带备份）
- 管理 cron 定时任务 + 一键重启
- 多机集群：每个节点一份 web UI，集群 tab 聚合其他节点状态
- 回放页：缩略图进度条、跨节点文件聚合、视频流代理（解决 HTTPS Mixed Content）

## 特性

- **零 pip 依赖** — `app.py` 用 `http.server` + `sqlite3`，任何 Python 3.8+ 都能跑
- **单服务（一个 systemd unit）** — 不依赖 Caddy / Nginx / Docker
- **跨平台** — RK3588 (aarch64) 自动装 RKMPP 硬编，x86 软编 fallback
- **单 SQLite** — 历史/队列/任务都在 `data/history.db`
- **多机集群** — 每节点独立，加节点跑一条命令
- **可选公网 HTTPS** — `install.sh --funnel` 一行启用 Tailscale Funnel

---

## 架构（重构后）

```
┌────────────────────────────────────────────────────────────────┐
│  Browser → http://<NAS-IP>:8765         (LAN / Tailscale)      │
│                                                                │
│  ┌──────────────────────────────────────────┐                  │
│  │  python3 app.py (ThreadingHTTPServer)    │  ONE service      │
│  │  ├─ /api/queue      任务列表/CRUD         │                  │
│  │  ├─ /api/run|stop   worker 控制           │                  │
│  │  ├─ /api/files      本机文件浏览          │                  │
│  │  ├─ /api/cluster/*  多机集群 (peer CRUD,  │                  │
│  │  │                 状态聚合,文件代理)     │                  │
│  │  └─ /static/*       SPA 前端              │                  │
│  └──────────────────────────────────────────┘                  │
│      │                                                         │
│      ├─→ SQLite (data/history.db)                              │
│      ├─→ /input → /output                                      │
│      ├─→ ffmpeg (rkmpp 优先, libx264 兜底)                      │
│      └─→ 其他 peer NAS (HTTP, 5s/30s 轮询)                     │
│                                                                │
│  可选 —— Tailscale Funnel → 公网 HTTPS                          │
│  (不需要公网 IP，不需要证书，自动 Tailscale 设备认证)            │
└────────────────────────────────────────────────────────────────┘
```

**重构前 → 重构后对比**：
- ❌ 6 个 systemd service（Caddy × 1, cluster-portal × 1, ofelia × 1, video-manager × 1, ...）
- ❌ 5 个 deploy 脚本,每个要 sudo 跑,改 4 个配置文件
- ❌ /etc/caddy/peers.conf 手编
- ✅ 1 个 service，1 个 install.sh，UI 改 peers

---

## 关键路径

| 角色 | 路径 |
|---|---|
| Web UI | `http://<NAS-IP>:8765` |
| 应用目录 | 默认 `/opt/video-manager`（`--prefix=PATH` 改） |
| 数据库 | `data/history.db`（gitignored） |
| 日志 | `logs/stdout.log` `logs/stderr.log` |
| 集群 peers | `data/peers.json` 或 `settings:cluster.peers`（DB） |
| 输入/输出 | 在 UI 配置 tab 改，或编辑 `app.py` 顶部 `INPUT_DIR`/`OUTPUT_DIR` |

---

## 安全

- 默认**无认证**，仅适合内网/受信任网络
- `video-manager.service` 已开 `NoNewPrivileges=true` + `ProtectSystem=full`
- `install.sh --funnel` 经 Tailscale 走：免 BasicAuth，Tailscale 设备身份即认证
- LAN 公网直接暴露前请套反代 + BasicAuth，或用 `tailscale serve`+ACL 限制

---

## 协议

[MIT](./LICENSE)

## 致谢

- [Ugreen](https://www.ugreen.com/) — DH4300Plus 硬件平台
- [Rockchip](https://www.rock-chips.com/) — MPP/RKRGA
- [Tailscale](https://tailscale.com/) — Funnel / 跨节点直连
- ffmpeg 项目和所有 encoder maintainer
