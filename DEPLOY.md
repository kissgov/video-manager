# 部署指南

把 video-manager 部署到另一台机器，分**两层**：

- **L1 — Web UI**：单文件 Python stdlib HTTP server，任何 Linux 都能跑。
- **L2 — 压缩管线**：`compress_video.sh` + Docker + ofelia 调度 + RKMPP 硬编，仅限 RK3588/Ugreen DH4300Plus。

> 不做 L2 也可以只跑 L1，UI 会显示"无任务"、日志/统计/文件浏览全可用，只是不会自动压缩。

---

## 0. 目标机器先决条件

| 项 | 要求 | 说明 |
|---|---|---|
| OS | Linux（任何带 systemd + Python 3.8+ 的发行版） | Debian/Ubuntu 推荐 |
| Python | 3.8+（标准库即可，**无需 pip install**） | `python3 --version` ≥ 3.8 |
| 用户 | 一个非 root 账户（建议沿用 `kxrdyf`） | `id kxrdyf` 确认存在 |
| 端口 | 8765 空闲（或改 `app.py` 里的 `PORT`） | |
| 磁盘 | 给 `data/`、`logs/`、`/input`、`/output` 留空间 | /input 可只读挂载 |
| Docker（仅 L2） | docker + compose plugin | `docker --version` 确认 |
| 设备文件（仅 RK3588） | `/dev/mpp_service`、`/dev/dma_heap/{system,cma}` | 绿联 OS 默认就有 |

---

## 1. 要复制的文件清单

从源机（当前 NAS）打包：

```bash
# 在源机上
cd /volume1/scripts
tar czf video-manager.tar.gz \
    video-manager/app.py \
    video-manager/static/ \
    video-manager/video-manager.service \
    compress_video.sh

# L2 还要带（不要落进 L1 tarball，避免混淆）
cd /volume1/docker
tar czf ffmpeg-stack.tar.gz \
    ffmpeg/Dockerfile \
    ffmpeg/docker-compose.yaml \
    ffmpeg/rebuild.sh \
    ffmpeg/setup.sh \
    ffmpeg/ofelia.ini
```

> `data/history.db` 和 `logs/` **不要复制**——新机重新建空 DB 即可。  
> 如果是迁移已有数据再单独 `scp data/history.db`。

---

## 2. 落地步骤（L1：Web UI 最小集）

```bash
# 1) 解包
mkdir -p /volume1/scripts
cd /volume1/scripts
tar xzf /path/to/video-manager.tar.gz
chown -R kxrdyf:admin /volume1/scripts/video-manager
chmod +x /volume1/scripts/compress_video.sh

# 2) 准备 /input 和 /output（两种方式任选）
#    方式 A: 软链接到实际目录
ln -s /your/camera/recordings /input
ln -s /your/compressed/videos /output
#    方式 B: 直接改 app.py 顶部
#      INPUT_DIR  = Path("/your/camera/recordings")
#      OUTPUT_DIR = Path("/your/compressed/videos")

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

L1 跑起来之后再做这几步。

### 3.1 用户加 video 组（关键，否则 RKMPP 起不来）

```bash
sudo usermod -aG video kxrdyf
# 重新登录或
newgrp video
groups kxrdyf   # 确认包含 video
```

### 3.2 安装带 RKMPP 的 ffmpeg

**方式 A — Docker 走 ffmpeg-worker（推荐，调度在容器里）**

```bash
# 把 ffmpeg-stack.tar.gz 解到标准位置
cd /volume1
tar xzf /path/to/ffmpeg-stack.tar.gz   # 解出 docker/ffmpeg/
cd /volume1/docker/ffmpeg

# 第一次构建（RK3588 上约 15-25 分钟）
sudo docker compose build ffmpeg-worker

# 起来
sudo docker compose up -d ffmpeg-worker ofelia-scheduler

# 验证 RKMPP 在容器内能用
sudo docker exec ffmpeg-worker ffmpeg -hide_banner -encoders 2>/dev/null | grep rkmpp
# 期望: 看到 h264_rkmpp / hevc_rkmpp
```

`docker-compose.yaml` 必须挂这几个：
- `/input`、`/output`（主机目录或 bind mount）
- `/volume1/scripts/compress_video.sh` → 容器内 `/scripts/compress_video.sh`
- `/dev/mpp_service`、`/dev/dma_heap/system`、`/dev/dma_heap/cma`（device cgroup）
- 用户的 `video` 组（用 `group_add`）

### 3.3 把脚本路径对齐

`compress_video.sh` 顶部几行检查顺序：
```bash
FFMPEG_BIN="/usr/local/bin/ffmpeg-rkmpp"      # 主机备用
FFMPEG_BIN="/usr/local/rkmpp/ffmpeg"         # 主机备用（"祭坛"）
FFMPEG_BIN="/ugreen/@appstore/.../ffmpeg"     # 绿联自带
FFMPEG_BIN="/usr/local/bin/ffmpeg"           # 最终兜底（通常没 rkmpp）
```
Docker 模式下容器内 `/usr/local/bin/ffmpeg` 就是带 rkmpp 的版本，自动命中。

### 3.4 ofelia 定时

容器起来后，2 AM 自动跑 `compress_video.sh`。修改 `/volume1/docker/ffmpeg/ofelia.ini` 后：

```bash
sudo docker restart ofelia-scheduler
```

Web UI"定时"标签页可以远程改 + 一键重启。

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
| 任务全 stuck 在 `pending` | `/input` 软链接断了？`ls -L /input \| head` |
| UI 全是 "—"（用时列空） | 见 `backfill_task_durations()`，POST `/api/queue/backfill_durations` |
| 压缩慢、软编 | `detect_hwaccel` 没拿到 RKMPP；用户不在 video 组 |
| ofelia 没跑 | `docker ps \| grep ofelia`；`docker logs ofelia-scheduler` |
| 端口冲突 | `ss -tlnp \| grep 8765`，改 `app.py` 的 `PORT` 常量后重启 |

---

## 6. 安全建议

- 当前**无认证**，仅内网。
- 外网访问前：套 Caddy/Nginx 反代 + BasicAuth，或前置 VPN（Tailscale/WireGuard）。
- `video-manager.service` 已开 `NoNewPrivileges=true` + `ProtectSystem=full`，无需再加固。
