#!/usr/bin/env bash
# video-manager 一键安装/升级/卸载
#
# 角色:
#   --primary (默认)   集群第一台
#   --worker --join=URL  加入集群
#   --update           拉最新 + 重启
#   --uninstall        干净卸载(保留 data/)
#   --funnel           同时开 Tailscale Funnel
#
# 路径:
#   --prefix=PATH      安装目录 (默认 /opt/video-manager)
#   --user=NAME        运行用户 (默认当前用户)
#   --port=PORT        监听端口 (默认 8765)
#
# 用法:
#   ./contrib/install.sh                                    # 主节点
#   ./contrib/install.sh --worker --join=http://100.x.x.x   # 加入集群
#   ./contrib/install.sh --primary --funnel                 # 主节点 + 公网 HTTPS
#   ./contrib/install.sh --update                           # 升级
#   ./contrib/install.sh --uninstall                        # 卸载
#
set -euo pipefail

# ---------- 默认值 ----------
INSTALL_USER="${INSTALL_USER:-$(id -un)}"
INSTALL_DIR="${INSTALL_DIR:-/opt/video-manager}"
SERVICE_NAME="${SERVICE_NAME:-video-manager}"
PORT="${PORT:-8765}"
GIT_REPO="${GIT_REPO:-https://github.com/kissgov/video-manager.git}"
GIT_BRANCH="${GIT_BRANCH:-main}"
SELF_ID="${SELF_ID:-}"   # 节点 id (默认用 hostname)

ROLE=""
JOIN_URL=""
ENABLE_FUNNEL=false
UPDATE=false
UNINSTALL=false
INPLACE=false

# ---------- 配色 (无障碍) ----------
G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[34m'; N=$'\033[0m'
log()  { echo "${B}[$(date +%H:%M:%S)]${N} $*"; }
ok()   { echo "${G}✓${N} $*"; }
warn() { echo "${Y}⚠${N}  $*"; }
err()  { echo "${R}✗${N}  $*" >&2; }

usage() {
  cat <<'EOF'
用法: install.sh [OPTIONS]

主节点 (默认):
  ./install.sh [--funnel]

加入集群:
  ./install.sh --worker --join=http://100.x.x.x:8765

其他:
  ./install.sh --update               # 拉最新 + 重启
  ./install.sh --uninstall            # 干净卸载(保留 data/)
  ./install.sh --prefix=/custom/path  # 自定义安装路径

EOF
}

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --primary)        ROLE="primary" ;;
    --worker)         ROLE="worker" ;;
    --join=*)         JOIN_URL="${1#*=}"; ROLE="worker" ;;
    --funnel)         ENABLE_FUNNEL=true ;;
    --update)         UPDATE=true ;;
    --uninstall)      UNINSTALL=true ;;
    --in-place)       INPLACE=true ;;
    --id=*)           SELF_ID="${1#*=}" ;;
    --user=*)         INSTALL_USER="${1#*=}" ;;
    --prefix=*)       INSTALL_DIR="${1#*=}" ;;
    --port=*)         PORT="${1#*=}" ;;
    --repo=*)         GIT_REPO="${1#*=}" ;;
    --branch=*)       GIT_BRANCH="${1#*=}" ;;
    -h|--help)        usage; exit 0 ;;
    *)                err "未知参数: $1"; usage; exit 1 ;;
  esac
  shift
done

[[ -z "$ROLE" ]] && ROLE="primary"
[[ "$ROLE" == "worker" && -z "$JOIN_URL" ]] && { err "--worker 需要 --join=URL"; exit 1; }

# ---------- 提权小工具 (需要在 uninstall 之前定义) ----------
sudo_q() {
  # 如果设了 SUDO_PASSWORD 环境变量,优先用 (用于非 TTY 环境如 cron/ agent 调用)
  if [[ -n "${SUDO_PASSWORD:-}" ]]; then
    # 用 here-string 避免 pipe 跨进程 stdin 问题
    sudo -S "$@" < <(printf '%s\n' "$SUDO_PASSWORD") 2>/dev/null
    return $?
  fi
  # 不然试 sudo -n: 优先免密
  if sudo -n true 2>/dev/null; then
    sudo -n "$@"
    return $?
  fi
  # 没有 TTY 也 无密码 → 报清晰错误让用户补两选一
  if ! [ -t 0 ]; then
    err "sudo 要密码,但这个 shell 没有 TTY"
    err "解决 1: sudo bash contrib/install-sudoers.sh   (装一次,以后免密)"
    err "解决 2: SUDO_PASSWORD=你的密码 bash $0 $*"
    exit 1
  fi
  sudo "$@"
}

# ---------- sudoers 规则检查 ----------
ensure_sudoers() {
  if sudo -n /usr/bin/systemctl restart "$SERVICE_NAME" >/dev/null 2>&1; then
    return 0
  fi
  if [ -f /etc/sudoers.d/video-manager ]; then
    warn "/etc/sudoers.d/video-manager 存在但 sudo 没免密 — 检查 sudoers 语法"
  fi
  if sudo_q true 2>/dev/null; then
    ok "sudo 可用(密码)"
  else
    err "装 sudoers: sudo bash contrib/install-sudoers.sh"
    exit 1
  fi
}

# ---------- 卸载 ----------
if [[ "$UNINSTALL" == true ]]; then
  log "🗑️  卸载 video-manager"
  sudo -n systemctl disable --now "$SERVICE_NAME" 2>/dev/null || sudo systemctl disable --now "$SERVICE_NAME" || true
  sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  sudo systemctl daemon-reload
  ok "systemd unit 已移除 (/etc/systemd/system/${SERVICE_NAME}.service)"
  if [[ -d "$INSTALL_DIR/data" ]]; then
    warn "数据目录保留: $INSTALL_DIR/data"
    warn "完全删除: sudo rm -rf $INSTALL_DIR"
  fi
  exit 0
fi

# ---------- 前置检查 ----------
log "🔍 预检"
[[ "$(id -un)" == "root" ]] && { err "请用普通用户跑 (不要 root) — 需要 sudo 的时候会问"; exit 1; }
command -v systemctl >/dev/null || { err "需要 systemd,本机没有 systemctl"; exit 1; }
command -v python3 >/dev/null  || { err "需要 python3"; exit 1; }
command -v sudo      >/dev/null || { err "需要 sudo"; exit 1; }
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
[[ "$(printf '%s\n3.8\n' "$PYVER" | sort -V | head -1)" == "3.8" ]] || { err "需要 Python ≥ 3.8 (你: $PYVER)"; exit 1; }
ok "Python $PYVER"
ensure_sudoers

# ---------- 安装路径 ----------
if [[ "$UPDATE" == true ]]; then
  log "🔄 升级模式"
  [[ -d "$INSTALL_DIR" ]] || { err "找不到 $INSTALL_DIR,先 --primary 或 --worker 安装一次"; exit 1; }
  cd "$INSTALL_DIR"
  if [[ -d .git ]]; then
    git fetch --depth=1 origin "$GIT_BRANCH" 2>/dev/null || true
    git reset --hard "origin/$GIT_BRANCH" 2>/dev/null || git pull --ff-only
    ok "已 pull 最新代码"
  else
    warn "$INSTALL_DIR 不是 git 仓库,跳过拉代码"
  fi
  sudo_q /usr/bin/systemctl restart "$SERVICE_NAME"
  ok "服务已重启"
  echo
  ok "升级完成 → http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT/"
  exit 0
fi

# 决定是否用现有目录
NEED_CLONE=true
if [[ -d "$INSTALL_DIR" ]]; then
  if [[ -f "$INSTALL_DIR/app.py" ]]; then
    warn "$INSTALL_DIR 已存在 video-manager,复用"
    NEED_CLONE=false
  fi
elif [[ "$INPLACE" == true ]] || [[ -f "./contrib/install.sh" && -f "./app.py" ]]; then
  # 当前目录就是源码 — in-place 安装
  INSTALL_DIR="$(pwd)"
  warn "in-place 安装,目标目录 = $INSTALL_DIR"
  NEED_CLONE=false
fi

if [[ "$NEED_CLONE" == true ]]; then
  log "📦 克隆 $GIT_REPO -> $INSTALL_DIR"
  command -v git >/dev/null || { err "需要 git"; exit 1; }
  sudo_q mkdir -p "$INSTALL_DIR"
  sudo_q chown "$INSTALL_USER:$INSTALL_USER" "$INSTALL_DIR"
  git clone --branch "$GIT_BRANCH" --depth 1 "$GIT_REPO" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
mkdir -p data logs
[[ -f data/history.db ]] || touch data/history.db
ok "目录: $INSTALL_DIR"

# ---------- 检测 RK3588 / ARM64 ----------
if [[ "$(uname -m)" == "aarch64" ]]; then
  if [[ -e /dev/mpp_service ]] && ! command -v ffmpeg-rkmpp >/dev/null 2>&1 && [[ ! -x /usr/local/bin/ffmpeg-rkmpp ]]; then
    log "🎬 检测到 RK3588 (aarch64 + /dev/mpp_service),装 RKMPP ffmpeg"
    if [[ -x contrib/setup-l2-rkmpp.sh ]]; then
      warn "需 root,会问 sudo 密码"
      sudo_q bash contrib/setup-l2-rkmpp.sh || warn "RKMPP 装失败,可手动重跑 contrib/setup-l2-rkmpp.sh"
    fi
  else
    log "🎬 ARM64 但 RKMPP 不可用或已装 — 跳过"
  fi
else
  log "💻 $(uname -m) — 软件编码足够,不装 RKMPP"
fi

# ---------- systemd unit ----------
log "⚙️  写 systemd unit"
SERVICE_FILE="/tmp/${SERVICE_NAME}.service.$$"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Video Compression Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$INSTALL_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/app.py
Environment=PORT=$PORT
Restart=on-failure
RestartSec=5
StandardOutput=append:$INSTALL_DIR/logs/stdout.log
StandardError=append:$INSTALL_DIR/logs/stderr.log
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=false

[Install]
WantedBy=multi-user.target
EOF

sudo_q mv "$SERVICE_FILE" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo_q /usr/bin/systemctl daemon-reload
sudo_q /usr/bin/systemctl enable --now "$SERVICE_NAME"
ok "systemd: $(sudo -n /usr/bin/systemctl is-active "$SERVICE_NAME" 2>/dev/null || sudo /usr/bin/systemctl is-active "$SERVICE_NAME")"

# 等服务就绪
for i in 1 2 3 4 5; do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/cluster/health" >/dev/null 2>&1; then
    ok "服务就绪 (port $PORT)"
    break
  fi
  sleep 1
  [[ $i -eq 5 ]] && warn "服务暂未回应 /api/cluster/health,可在 journal 里看"
done

# ---------- worker: 加入集群 ----------
if [[ "$ROLE" == "worker" ]]; then
  SELF_HOSTNAME=$(hostname -s)
  [[ -n "$SELF_ID" ]] || SELF_ID="$SELF_HOSTNAME"
  SELF_TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || echo "")
  SELF_LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  SELF_IP="${SELF_TS_IP:-$SELF_LAN_IP}"
  SELF_URL="http://${SELF_IP}:$PORT"

  log "🔗 注册本节点 ($SELF_ID) 到集群: $JOIN_URL"
  if curl -fsS -X POST -H "Content-Type: application/json" \
      -d "{\"id\":\"$SELF_ID\",\"url\":\"$SELF_URL\",\"name\":\"$SELF_ID\"}" \
      "$JOIN_URL/api/cluster/peers/$SELF_ID" 2>&1; then
    ok "已加入集群 (在 primary 上: http://$(echo "$JOIN_URL" | sed 's|http://||;s|:.*||'):$PORT/ → 集群 tab)"
  else
    warn "远程注册失败,可在 primary UI 上手动加: id=$SELF_ID url=$SELF_URL"
  fi
fi

# ---------- 可选: Tailscale Funnel ----------
if [[ "$ENABLE_FUNNEL" == true ]]; then
  if command -v tailscale >/dev/null; then
    log "🌐 启用 Tailscale Funnel:$PORT"
    if sudo_q tailscale funnel --bg --yes "$PORT" 2>&1; then
      FUNNEL_URL=$(tailscale funnel --bg --yes status 2>/dev/null | grep -oE 'https://[^ ]+' | head -1 || true)
      ok "Funnel: ${FUNNEL_URL:-<检查 tailscale funnel>}"
    else
      warn "Funnel 启用失败: 去 https://login.tailscale.com/admin → Funnel 启用"
    fi
  else
    warn "没装 tailscale,跳过 Funnel"
  fi
fi

# ---------- 收尾 ----------
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)

cat <<EOF

${G}✅ 安装完成!${N}

  本机 LAN:    http://${LAN_IP:-?}:$PORT/
$([ -n "$TS_IP" ] && echo "  Tailscale:  http://$TS_IP:$PORT/")
$([ "$ENABLE_FUNNEL" == true ] && echo "  公网 HTTPS: (Funnel URL 见上面)")
$([ "$ROLE" == "worker" ] && echo "  角色:       worker (加入 $JOIN_URL)")

  常用命令:
    systemctl status $SERVICE_NAME   # 看状态
    journalctl -u $SERVICE_NAME -f   # 看日志
    $0 --update                      # 升级 (拉最新 + 重启)
    $0 --uninstall                   # 卸载 (保留数据)

EOF
