#!/usr/bin/env bash
# 在 NAS 上装 Caddy 门户 + cluster-portal 管理服务 (无 BasicAuth)
#
# 用法: sudo ./portal-setup.sh
#
# 流程:
#   1) apt install caddy (如果没有)
#   2) 部署 cluster-portal.py + .html + .service 到 /etc/caddy/cluster-portal/
#   3) 读 peers.conf 生成后端路由(可注释掉暂时不存在的 NAS)
#   4) 替换 Caddyfile 模板里的占位符
#   5) 写到 /etc/caddy/Caddyfile
#   6) caddy validate + systemctl reload
#   7) 启动 cluster-portal (负责动态首页 + 管理 API)
#
# 文件:
#   - Caddyfile.template: 含 __BACKENDS__ 占位符
#   - cluster-portal.py / .html: 动态门户 (port 8889)
#   - cluster-portal.service: systemd unit
#   - peers.conf: 每行一个 NAS,格式 id=url
#     例:
#       nas1=http://127.0.0.1:8765
#       #nas2=http://100.x.0.12:8765
#       #nas3=http://100.x.0.13:8765
#
# 访问:
#   http://<NAS-IP>/         → 集群首页(动态 SPA, 可管理 NAS)
#   http://<NAS-IP>/nas1/    → NAS-1 video-manager
#   http://<NAS-IP>/nas2/    → NAS-2 (如果配了)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$SCRIPT_DIR/Caddyfile.template"
PEERS_CONF="$SCRIPT_DIR/peers.conf"
OUTPUT="/etc/caddy/Caddyfile"
CP_DIR="/etc/caddy/cluster-portal"
CP_SERVICE="/etc/systemd/system/cluster-portal.service"

if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root" >&2
    exit 1
fi

# 装 caddy (Debian 12 仓库就有)
if ! command -v caddy >/dev/null; then
    echo "==> 安装 caddy..."
    apt-get update
    apt-get install -y --no-install-recommends caddy
fi

# 部署 cluster-portal (动态首页 + 管理 API)
echo "==> 部署 cluster-portal 到 $CP_DIR"
mkdir -p "$CP_DIR"
install -m 0755 "$SCRIPT_DIR/cluster-portal.py"   "$CP_DIR/cluster-portal.py"
install -m 0644 "$SCRIPT_DIR/cluster-portal.html"  "$CP_DIR/cluster-portal.html"
install -m 0644 "$SCRIPT_DIR/peers.conf"          "$CP_DIR/peers.conf"
install -m 0644 "$SCRIPT_DIR/cluster-portal.service" "$CP_SERVICE"
systemctl daemon-reload
systemctl enable --now cluster-portal
sleep 1
systemctl is-active cluster-portal >/dev/null || {
    echo "❌ cluster-portal 启动失败" >&2
    journalctl -u cluster-portal -n 20 --no-pager >&2
    exit 1
}

# 读 peers.conf 生成后端路由
echo "==> 读取 $PEERS_CONF"
if [ ! -f "$PEERS_CONF" ]; then
    echo "❌ 找不到 $PEERS_CONF" >&2
    exit 1
fi

BACKENDS=""
PEER_IDS=()
while IFS= read -r line; do
    # 跳过注释和空行
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line// /}" ]] && continue
    # 解析 id=url
    if [[ "$line" =~ ^([^=]+)=(.+)$ ]]; then
        pid="${BASH_REMATCH[1]}"
        url="${BASH_REMATCH[2]}"
        pid=$(echo "$pid" | xargs)
        url=$(echo "$url" | xargs)
        PEER_IDS+=("$pid")
        BACKENDS+="
    handle_path /$pid/* {
        reverse_proxy $url
    }"
    fi
done < "$PEERS_CONF"

if [ ${#PEER_IDS[@]} -eq 0 ]; then
    echo "❌ peers.conf 里没有可用 peer" >&2
    exit 1
fi

# 生成首页 HTML(动态)
HOME_CARDS=""
for pid in "${PEER_IDS[@]}"; do
    url=$(grep "^${pid}=" "$PEERS_CONF" | head -1 | cut -d= -f2-)
    HOME_CARDS+="
  <a class=\"card\" href=\"/$pid/\">
    <div class=\"title\">$pid</div>
    <div class=\"meta\">$url</div>
  </a>"
done

# 不设 BasicAuth (Tailscale Funnel 走设备认证, LAN 假设信任)
AUTH_USER=""
HASH=""
echo "==> 跳过 BasicAuth (无认证,Tailscale/LAN 信任)"

# 替换占位符 (用唯一标记,不会被误替换)
echo "==> 生成 Caddyfile"
sed -e "s|__HOME_CARDS__|$HOME_CARDS|g" \
    -e "s|__BACKENDS__|$BACKENDS|g" \
    "$TEMPLATE" > "$OUTPUT"

echo "==> 验证 Caddyfile"
if ! caddy validate --config "$OUTPUT" --adapter caddyfile 2>/tmp/caddy-validate.log; then
    echo "❌ Caddyfile 有问题:" >&2
    cat /tmp/caddy-validate.log
    exit 1
fi

echo "==> 启动/重载 caddy"
systemctl enable --now caddy
systemctl reload caddy
sleep 1
systemctl status caddy --no-pager -l | head -10

echo ""
echo "=== 验证 ==="
echo "curl http://127.0.0.1:8888/                          # 集群首页 (cluster-portal SPA)"
echo "curl http://127.0.0.1:8888/api/cluster/nodes         # 节点列表 + 实时状态"
echo "curl http://127.0.0.1:8888/nas1/                      # NAS-1 反代"
echo "预期:${#PEER_IDS[@]} 个 NAS 节点, 实时状态, 可加/禁/删 (无 BasicAuth)"
echo ""
echo "=== peers.conf 示例 (部署新 NAS 时取消注释) ==="
cat <<'EOF'
  # 当前活跃:
  nas1=http://127.0.0.1:8765

  # 待部署 (取消注释 + 填真实 Tailscale IP):
  #nas2=http://100.x.0.12:8765
  #nas3=http://100.x.0.13:8765
  #nas4=http://100.x.0.14:8765
EOF