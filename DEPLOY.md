# 部署指南

把 video-manager 部署到另一台机器，分**两层**：

- **L1 — Web UI**：模块化 `server/` 包 + React SPA 前端，Python 标准库零依赖，任何 Linux 都能跑。
- **L2 — 压缩管线**：Python worker 线程直接调用 ffmpeg（RKMPP 硬编优先），内置 cron 调度器，无需 Docker/ofelia。仅限 RK3588/Ugreen DH4300Plus 需装 RKMPP ffmpeg。

> 不做 L2 也可以只跑 L1，UI 会显示"无任务"、日志/统计/文件浏览全可用，只是不会自动压缩。

---

## 0. 目标机器先决条件

| 项 | 要求 | 说明 |
|---|---|---|
| OS | Linux（任何带 systemd + Python 3.8+ 的发行版） | Debian/Ubuntu 推荐 |
| Python | 3.8+（标准库即可，**无需 pip install**） | `python3 --version` ≥ 3.8 |
| 用户 | 一个非 root 账户（建议沿用 `kxrdyf`） | `id kxrdyf` 确认存在 |
| 端口 | 8765 空闲（或设 `PORT` 环境变量） | |
| 磁盘 | 给 `data/`、`logs/`、`/input`、`/output` 留空间 | /input 可只读挂载 |
| ffmpeg（仅 L2） | 系统自带或装 RKMPP 版 | Python worker 自动探测最优编码器 |
| 设备文件（仅 RK3588） | `/dev/mpp_service`、`/dev/dma_heap/{system,cma}` | 绿联 OS 默认就有 |

---

## 1. 要复制的文件清单

推荐用 `git clone`（最简），或手动打包：

```bash
# 方式 A: git clone（推荐）
git clone https://github.com/kissgov/video-manager.git

# 方式 B: 手动打包
cd /volume1/scripts
tar czf video-manager.tar.gz \
    video-manager/app.py \
    video-manager/server/ \
    video-manager/static/dist/ \
    video-manager/static/src/ \
    video-manager/package.json \
    video-manager/video-manager.service \
    video-manager/contrib/
```

> `data/history.db` 和 `logs/` **不要复制**——新机重新建空 DB 即可。  
> 如果是迁移已有数据再单独 `scp data/history.db`。  
> `static/dist/` 是预构建的 React 产物，**部署时不需要 Node.js / npm**。

---

## 2. 落地步骤（L1：Web UI 最小集）

```bash
# 1) 解包
mkdir -p /volume1/scripts
cd /volume1/scripts
tar xzf /path/to/video-manager.tar.gz
chown -R kxrdyf:admin /volume1/scripts/video-manager

# 2) 准备 /input 和 /output（三种方式任选）
#    方式 A: 软链接到实际目录
ln -s /your/camera/recordings /input
ln -s /your/compressed/videos /output
#    方式 B: 设环境变量（在 systemd unit 或启动脚本里）
#      VIDEO_MANAGER_INPUT_DIR=/your/camera/recordings
#      VIDEO_MANAGER_OUTPUT_DIR=/your/compressed/videos
#    方式 C: 启动后在 UI → 配置 tab 改路径（热加载，无需重启）

# 3) 装 systemd 服务
sudo cp /volume1/scripts/video-manager/video-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now video-manager

# 4) 防火墙放行 8765（如果有 ufw/firewalld）
sudo ufw allow from 192.168.0.0/16 to any port 8765   # 按需缩窄

# 5) 验证
curl -s http://127.0.0.1:8765/api/queue/stats
# 期望: {"pending":0,"running":0,"done":0,"failed":0,"skipped":0,"total":0}

# 浏览器访问 http://NAS-IP:8765
```

### 2.1 没有 systemd 的机器

```bash
cd /volume1/scripts/video-manager
nohup python3 app.py > logs/stdout.log 2>&1 &
echo $! > /tmp/video-manager.pid

# 停止
kill $(cat /tmp/video-manager.pid)
```

---

## 3. 落地步骤（L2：RK3588 完整管线）

L1 跑起来之后再做这几步。Python worker 直接在进程内调用 ffmpeg，无需 Docker。

### 3.1 用户加 video 组（关键，否则 RKMPP 起不来）

```bash
sudo usermod -aG video kxrdyf
# 重新登录或
newgrp video
groups kxrdyf   # 确认包含 video
```

### 3.2 安装带 RKMPP 的 ffmpeg

```bash
# 一键安装 jellyfin-ffmpeg (带 RKMPP) + cron 定时触发
sudo bash contrib/setup-l2-rkmpp.sh

# 验证
/usr/local/bin/ffmpeg-rkmpp -hide_banner -encoders 2>/dev/null | grep rkmpp
# 期望: 看到 h264_rkmpp / hevc_rkmpp
```

安装脚本会自动配置 cron（每天 02:00 调 `/api/run` 触发 Python worker）。

也可以在 Web UI"定时"标签页用内置 cron 调度器管理定时任务（不依赖系统 cron）。

### 3.3 ffmpeg 探测优先级

Python worker 启动时自动探测最优 ffmpeg（`server/ffmpeg.py` 的 `_resolve_ffmpeg_bin`）：

1. `/usr/local/bin/ffmpeg-rkmpp`（setup-l2-rkmpp.sh 装的）
2. `/usr/local/rkmpp/ffmpeg`
3. 绿联自带 `/ugreen/@appstore/.../ffmpeg`
4. 系统 `ffmpeg`（兜底，通常无 rkmpp）

探测结果在 UI → 系统标签页可见。

### 3.4 定时任务

两种方式任选：

- **内置调度器**（推荐）：UI → 定时标签页，直接增删 cron 规则，Python 调度线程每 30 秒检查一次。
- **系统 cron**：setup-l2-rkmpp.sh 装的 `/etc/cron.d/video-manager`，每天 02:00 调 `/api/run`。

> 旧的 Docker + ofelia 方式仍兼容（`server/ofelia.py` 管理 ofelia.ini），但新部署不再需要。

---

## 4. 跨机迁移已有数据

```bash
# 源机：导出 SQLite
sqlite3 /volume1/scripts/video-manager/data/history.db ".backup '/tmp/h.db'"
scp /tmp/h.db new-nas:/tmp/

# 目标机：覆盖（先停服务）
sudo systemctl stop video-manager
cp /volume1/scripts/video-manager/data/history.db \
   /volume1/scripts/video-manager/data/history.db.bak.$(date +%s)
cp /tmp/h.db /volume1/scripts/video-manager/data/history.db
chown kxrdyf:admin /volume1/scripts/video-manager/data/history.db
sudo systemctl start video-manager
```

---

## 5. 故障排查清单

| 现象 | 查什么 |
|---|---|
| UI 502 / 连接拒绝 | `systemctl status video-manager` + `tail logs/app.log` |
| `/api/queue` 长时间无响应 | DB 锁；`lsof data/history.db` 看有没有遗留进程 |
| 任务全 stuck 在 `pending` | `/input` 路径不对？UI → 配置 tab 检查 INPUT_DIR |
| UI 全是 "—"（用时列空） | POST `/api/queue/backfill_durations` 回填时长 |
| 压缩慢、软编 | UI → 系统 tab 看 ffmpeg 路径；用户不在 video 组则 RKMPP 不可用 |
| 定时没跑 | UI → 定时 tab 检查调度器状态；或 `tail /var/log/syslog \| grep cron` |
| 端口冲突 | `ss -tlnp \| grep 8765`，设 `PORT` 环境变量后重启 |

---

## 6. 安全建议

- 当前**无认证**，仅内网。
- 外网访问前：套 Caddy/Nginx 反代 + BasicAuth，或前置 VPN（Tailscale/WireGuard）。
- `video-manager.service` 已开 `NoNewPrivileges=true` + `ProtectSystem=full`，无需再加固。
