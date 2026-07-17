#!/usr/bin/env bash
# 在 NAS-1 上装 Caddy 门户,带 BasicAuth
#
# 用法: sudo ./portal-setup.sh
#
# 流程:
#   1) apt install caddy
#   2) 让你输入用户名/密码,用 caddy hash-password 生成 bcrypt
#   3) 替换 Caddyfile 模板里的 PLACEHOLDER_HASH
#   4) 写到 /etc/caddy/Caddyfile
#   5) systemctl reload caddy
#
# 注意:
#   - 需要解析到本机的域名(nas.example.com),Caddy 自动 HTTPS
#   - 或者改用 Tailscale Funnel 暴露(无需公网)
#   - Tailscale IP 在 Caddyfile 里硬编码,部署新 NAS 时要改

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$SCRIPT_DIR/Caddyfile.template"
OUTPUT="/etc/caddy/Caddyfile"

if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root" >&2
    exit 1
fi

# 装 caddy
if ! command -v caddy >/dev/null; then
    echo "==> 安装 caddy..."
    apt-get update
    apt-get install -y --no-install-recommends caddy
fi

# 生成密码哈希
echo ""
echo "==> 设置 BasicAuth"
read -rp "  用户名 [admin]: " AUTH_USER
AUTH_USER="${AUTH_USER:-admin}"
echo "  请输入密码（caddy 会弹出 hash-password 提示）"
HASH=$(caddy hash-password --plaintext 2>/dev/null || {
    # 旧版 caddy 没 --plaintext, 走交互式
    caddy hash-password
})
if [ -z "$HASH" ]; then
    echo "❌ hash 生成失败" >&2
    exit 1
fi

# 替换占位符
echo "==> 写 Caddyfile 到 $OUTPUT"
sed -e "s/PLACEHOLDER_HASH/${HASH//\//\\/}/g" \
    "$TEMPLATE" > "$OUTPUT"

# 改用户名
sed -i "s/admin PLACEHOLDER_HASH/${AUTH_USER} ${HASH}/g" "$OUTPUT"

echo "==> 验证 Caddyfile..."
if ! caddy validate --config "$OUTPUT" --adapter caddyfile 2>/tmp/caddy-validate.log; then
    echo "❌ Caddyfile 有问题:" >&2
    cat /tmp/caddy-validate.log
    exit 1
fi
echo "    OK"

# 启动/重载
echo "==> 重启 caddy..."
systemctl enable --now caddy
systemctl reload caddy
sleep 1
systemctl status caddy --no-pager -l | head -10

echo ""
echo "=== 验证 ==="
echo "curl -u $AUTH_USER:<密码> http://127.0.0.1:80/"
echo "预期:返回 HTML,4 个 NAS 卡片"
echo ""
echo "=== 下一步 ==="
echo "1. 域名解析:把 nas.example.com A 记录指向 NAS-1 公网 IP(或 CNAME)"
echo "   或者用 Tailscale Funnel: sudo tailscale funnel 443 on"
echo "2. 其他 NAS 装 Tailscale: curl -fsSL https://tailscale.com/install.sh | sh"
echo "3. 编辑 $OUTPUT 把 100.x.0.12/13/14 改成实际 Tailscale IP"
echo "   sudo tailscale funnel 443 on"