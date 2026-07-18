#!/usr/bin/env bash
# 把 contrib/Caddyfile.template 渲染到 /etc/caddy/Caddyfile
# 同时装好 cluster-portal 服务 (如果还没装)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$SCRIPT_DIR/Caddyfile.template"
PEERS_CONF="$SCRIPT_DIR/peers.conf"
OUTPUT="${1:-/etc/caddy/Caddyfile}"

if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root" >&2; exit 1
fi

# 读 peers.conf, 生成 handle_path 块
BACKENDS=""
PEER_IDS=()
while IFS= read -r line; do
    # 跳过注释和空行
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line// /}" ]] && continue
    if [[ "$line" =~ ^([^=]+)=(.+)$ ]]; then
        pid=$(echo "${BASH_REMATCH[1]}" | xargs)
        url=$(echo "${BASH_REMATCH[2]}" | xargs)
        PEER_IDS+=("$pid")
        BACKENDS="${BACKENDS}    handle_path /${pid}/* {
        reverse_proxy ${url}
    }
"
    fi
done < "$PEERS_CONF"

if [ ${#PEER_IDS[@]} -eq 0 ]; then
    echo "❌ peers.conf 里没有可用 peer" >&2; exit 1
fi

# BACKENDS 多行含 /, 用 sed r 命令从文件读 (避免 s 命令里的 / delim 问题)
printf '%s' "$BACKENDS" > /tmp/_backends.txt
sed -e "/__BACKENDS__/{
    r /tmp/_backends.txt
    d
}" "$TEMPLATE" > "$OUTPUT.tmp"
mv "$OUTPUT.tmp" "$OUTPUT"
rm -f /tmp/_backends.txt
echo "✓ Caddyfile 渲染: $OUTPUT (${#PEER_IDS[@]} peers)"
