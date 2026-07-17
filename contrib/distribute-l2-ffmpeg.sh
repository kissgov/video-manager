#!/usr/bin/env bash
# 把本机编好的 ffmpeg-rkmpp 二进制 + cron 推到其他 NAS
#
# 用法: ./distribute-l2-ffmpeg.sh user@nas2-host [user@nas3-host ...]
#
# 兼容性:动态链接 libc/libmpp/librga，glibc 必须 >= 本机版本
#          否则 fallback 到目标机本地编译(走 setup-l2-rkmpp.sh)

set -euo pipefail

LOCAL_FFMPEG="/usr/local/bin/ffmpeg-rkmpp"
SUDOERS_FILE="/etc/sudoers.d/video-manager-restart"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ $# -eq 0 ]; then
    echo "用法: $0 user@nas2 [user@nas3 ...]" >&2
    echo "     需要每台 NAS 已装 Tailscale 并加入同一 tailnet" >&2
    exit 1
fi

if [ ! -x "$LOCAL_FFMPEG" ]; then
    echo "❌ 本机 $LOCAL_FFMPEG 不存在,先跑 setup-l2-rkmpp.sh" >&2
    exit 1
fi

# 收集要分发的依赖（动态链接的 .so）
log_deps() {
    ldd "$LOCAL_FFMPEG" 2>/dev/null | awk '/=>/ {print $3}' | sort -u
}

# 测目标机器 libc 版本兼容性
check_glibc_compat() {
    local remote="$1"
    local local_glibc local_arch remote_glibc remote_arch
    local_glibc=$(ldd --version 2>/dev/null | head -1 | awk '{print $NF}')
    local_arch=$(uname -m)
    remote_glibc=$(ssh "$remote" 'ldd --version 2>/dev/null | head -1 | awk "{print \$NF}"' 2>/dev/null || echo "?")
    remote_arch=$(ssh "$remote" 'uname -m' 2>/dev/null || echo "?")
    log "[$remote] glibc: $remote_glibc (本机: $local_glibc), arch: $remote_arch (本机: $local_arch)"
    # 简单判断:远端 glibc >= 本机 && 同架构
    if [ "$remote_arch" = "$local_arch" ] && [ "$remote_glibc" != "?" ]; then
        # 用 sort -V 比较版本
        if printf '%s\n%s\n' "$local_glibc" "$remote_glibc" | sort -V | tail -1 | grep -qx "$remote_glibc"; then
            return 0
        fi
    fi
    return 1
}

install_remote() {
    local remote="$1"
    log ""
    log "==> $remote"

    # 1. 推送二进制（scp,默认端口 22;走 Tailscale 100.x 互联）
    log "    [1/4] 推送 $LOCAL_FFMPEG ..."
    ssh "$remote" "sudo mkdir -p /usr/local/bin"
    scp "$LOCAL_FFMPEG" "${remote}:/tmp/ffmpeg-rkmpp"

    # 2. 试运行（检查依赖,可能缺 .so）
    log "    [2/4] 试运行,看依赖能不能找到..."
    set +e
    REMOTE_LDD_OUT=$(ssh "$remote" "sudo /tmp/ffmpeg-rkmpp -version 2>&1 | head -3")
    set -e
    if echo "$REMOTE_LDD_OUT" | grep -q "error while loading shared libraries"; then
        log "    ⚠️  远端缺 .so,需要本地编译"
        ssh "$remote" "rm -f /tmp/ffmpeg-rkmpp"
        return 1
    fi
    if ! echo "$REMOTE_LDD_OUT" | grep -q "ffmpeg version"; then
        log "    ⚠️  远端跑不起来: $REMOTE_LDD_OUT"
        return 1
    fi

    # 3. 装到 /usr/local/bin
    log "    [3/4] 安装到 $LOCAL_FFMPEG ..."
    ssh "$remote" "sudo mv /tmp/ffmpeg-rkmpp $LOCAL_FFMPEG && sudo chmod +x $LOCAL_FFMPEG"

    # 4. 验证 + 配 cron
    log "    [4/4] 配 cron ..."
    CRON_FILE="/etc/cron.d/video-manager"
    CRON_BODY=$(cat <<EOF
# 每天 02:00 触发 video-manager 自动转码
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 2 * * * $remote_user curl -fsS -X POST http://127.0.0.1:8765/api/run \\
    -H 'Content-Type: application/json' \\
    -d '{"trigger":"cron"}' \\
    >> /home/$remote_user/scripts/video-manager/logs/cron.log 2>&1
EOF
)
    local remote_user="${remote%%@*}"
    ssh "$remote" "echo '$CRON_BODY' | sudo tee $CRON_FILE > /dev/null && sudo chmod 644 $CRON_FILE && sudo systemctl restart cron 2>/dev/null || sudo systemctl restart crond 2>/dev/null"

    log "    ✅ $remote 完成"
    return 0
}

for remote in "$@"; do
    remote_user="${remote%%@*}"
    if check_glibc_compat "$remote"; then
        log "[$remote] glibc/arch 兼容,尝试推送二进制..."
        if install_remote "$remote"; then
            continue
        fi
    fi
    log "[$remote] 二进制不兼容,fallback 到本地编译..."
    log "    ssh $remote 'sudo $SCRIPT_DIR/setup-l2-rkmpp.sh'"
    ssh -t "$remote" "sudo $SCRIPT_DIR/setup-l2-rkmpp.sh" || log "    ❌ $remote 本地编译失败,需要手动处理"
done

log ""
log "全部完成 ✅"