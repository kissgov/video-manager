#!/usr/bin/env bash
# 在新 NAS 上部署 video-manager (复用 NAS-1 的代码)
# 用法: ./deploy-to-nas.sh user@nas-host [remote-port]
#
# 前置:
#   - 目标机器已装 python3、git
#   - 已配好 Tailscale（或者有公网 SSH）
#   - 你有这台机器的 sudo 密码（脚本会要求输入）
#
# 行为:
#   1. 在目标机器 git clone video-manager（或 rsync 从本地推）
#   2. 检测 /volume1 /home 布局，自动调整路径
#   3. sudo 安装 systemd unit + sudoers
#   4. 启动并 curl 验证

set -euo pipefail

REMOTE="${1:?用法: $0 user@nas-host [ssh-port]}"
SSH_PORT="${2:-22}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="video-manager"

echo "==> 源目录: $REPO_DIR"
echo "==> 目标: $REMOTE (port $SSH_PORT)"
echo

# 1) 推代码（rsync 比 git clone 快，也避免重新下载）
echo "==> 1) 推送代码到目标"
ssh -p "$SSH_PORT" "$REMOTE" "mkdir -p ~/scripts"
rsync -az --delete \
  -e "ssh -p $SSH_PORT" \
  --exclude='.git/' \
  --exclude='data/' \
  --exclude='logs/' \
  --exclude='*.db' \
  --exclude='*.bak' \
  "$REPO_DIR/" \
  "$REMOTE:~/scripts/video-manager/"

# 2) 检测布局 + 调路径
echo "==> 2) 检测布局 + 调路径"
ssh -p "$SSH_PORT" -t "$REMOTE" bash <<'REMOTE_EOF'
set -e
cd ~/scripts/video-manager

# 检测 /volume1 是不是符号链接或不存在
if [ -e /volume1 ] && [ ! -L /volume1 ]; then
  BASE="/volume1/scripts"
  echo "    [布局] 标准 (/volume1 是真挂载点)"
else
  BASE="/home/$(whoami)/scripts"
  echo "    [布局] /volume1 不可用,改用 $BASE"
fi

# 改硬编码路径
sed -i \
  -e "s|/volume1/scripts/video-manager|$BASE/video-manager|g" \
  -e "s|/volume1/scripts/compress|$BASE/compress|g" \
  -e "s|/volume1/docker/ffmpeg|$BASE/../docker/ffmpeg|g" \
  app.py video-manager.service

# 改 /input /output -> 用户家目录下（没 root 建不了根级）
sed -i 's|Path("/input")|Path(f"{Path.home()}/input")|g' app.py
sed -i 's|Path("/output")|Path(f"{Path.home()}/output")|g' app.py

# 修正 _LOCK_FILE 同 bug
sed -i 's|Path("/scripts/compress.lock")|Path(f"{Path.home()}/scripts/compress.lock")|g' app.py

# service 单元去 ProtectHome=read-only（这台机器 volume1 不在 /home 的话按需）
# 这里保守：直接关掉
sed -i 's|ProtectHome=read-only|ProtectHome=false|' video-manager.service

mkdir -p data logs input output
touch input/.keep output/.keep
python3 -m py_compile app.py && echo "    [编译] OK"

# 默认路径占位（用户后续在 UI 里改）
sed -i "s|_INPUT_DIR_DEFAULT\s*=.*|_INPUT_DIR_DEFAULT  = Path.home() / \"input\"|" app.py
sed -i "s|_OUTPUT_DIR_DEFAULT\s*=.*|_OUTPUT_DIR_DEFAULT = Path.home() / \"output\"|" app.py
REMOTE_EOF

# 3) sudo 装 systemd 单元
echo "==> 3) 装 systemd service + sudoers（要 sudo 密码）"
ssh -p "$SSH_PORT" -t "$REMOTE" bash <<'REMOTE_EOF'
set -e
cd ~/scripts/video-manager

sudo cp video-manager.service /etc/systemd/system/video-manager.service
sudo cp contrib/video-manager-restart.sudoers /etc/sudoers.d/video-manager-restart
sudo chmod 0440 /etc/sudoers.d/video-manager-restart
sudo visudo -c -f /etc/sudoers.d/video-manager-restart
sudo systemctl daemon-reload
sudo systemctl enable --now video-manager
sleep 1
sudo systemctl status video-manager --no-pager -l | head -5

echo "==> 4) 验证"
curl -sS http://127.0.0.1:8765/api/queue/stats || echo "FAIL"
echo
curl -sS http://127.0.0.1:8765/api/system 2>/dev/null | python3 -m json.tool 2>/dev/null | head -10
REMOTE_EOF

echo
echo "✅ 部署完成"
echo "   Tailscale IP: $(ssh -p $SSH_PORT $REMOTE 'tailscale ip -4 2>/dev/null || echo 未装 tailscale')"
echo "   Web UI:       http://<tailscale-ip>:8765"