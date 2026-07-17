#!/usr/bin/env bash
# 在 RK3588 / DH4300Plus 上装 RKMPP 硬编 ffmpeg + cron 自动转码
#
# 实测路径(Ugreen OS DH4300Plus bookworm 实跑过):
#   1) 从 gh-proxy.com 拉 jellyfin-ffmpeg7 (bookworm arm64 .deb, 15MB)
#   2) dpkg 装 → 复制到 /usr/local/bin/ffmpeg-rkmpp
#   3) 加 cron,每天 2 点 curl /api/run
#   4) video-manager 自动用上 (它按 _resolve_ffmpeg_bin() 优先级找)
#
# 用法: sudo ./setup-l2-rkmpp.sh

set -euo pipefail

INSTALL_PATH="/usr/local/bin/ffmpeg-rkmpp"
TMP_DEB="/tmp/jellyfin-ffmpeg.deb"
GHPROXY_URL="https://gh-proxy.com/https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v7.1.4-1/jellyfin-ffmpeg7_7.1.4-1-bookworm_arm64.deb"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ---------- 检查 ----------
if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root" >&2
    exit 1
fi

if [ ! -e /dev/mpp_service ]; then
    log "⚠️  /dev/mpp_service 不在,可能不是 RK3588" >&2
fi

# ---------- 下载 (走 gh-proxy,GitHub 在国内不稳) ----------
log "下载 jellyfin-ffmpeg (bookworm arm64)..."
if [ ! -f "$TMP_DEB" ] || [ "$(stat -c%s "$TMP_DEB" 2>/dev/null || echo 0)" -lt 1000000 ]; then
    curl -fL --retry 5 --retry-delay 3 \
         --connect-timeout 15 --max-time 1800 \
         -o "$TMP_DEB" \
         "$GHPROXY_URL"
fi

ACTUAL_SIZE=$(stat -c%s "$TMP_DEB")
if [ "$ACTUAL_SIZE" -lt 1000000 ]; then
    log "❌ 下载文件太小 ($ACTUAL_SIZE 字节),可能失败"
    exit 1
fi
log "已下载 $(numfmt --to=iec $ACTUAL_SIZE)"

# ---------- 装 ----------
log "dpkg -i ..."
DEBIAN_FRONTEND=noninteractive dpkg -i "$TMP_DEB" 2>&1 | tail -3

# 找到 jellyfin-ffmpeg 实际安装位置(可能在 /usr/lib/jellyfin-ffmpeg/ffmpeg)
JFMPEG_SRC=$(find /usr/lib/jellyfin-ffmpeg -name ffmpeg -type f 2>/dev/null | head -1)
if [ -z "$JFMPEG_SRC" ]; then
    log "❌ 找不到 jellyfin-ffmpeg 二进制"
    exit 1
fi

# 复制自带 lib(动态链接到 jellyfin-ffmpeg 自带 .so)
JFMPEG_LIB_DIR=/usr/lib/jellyfin-ffmpeg
for so in "$JFMPEG_LIB_DIR"/*.so*; do
    [ -e "$so" ] || continue
    cp -P "$so" /usr/local/lib/ 2>/dev/null || true
done
ldconfig

cp "$JFMPEG_SRC" "$INSTALL_PATH"
chmod +x "$INSTALL_PATH"
log "已装到 $INSTALL_PATH"

# ---------- 验证 ----------
log "验证 RKMPP 编器..."
if "$INSTALL_PATH" -hide_banner -encoders 2>/dev/null | grep -qE "h264_rkmpp|hevc_rkmpp"; then
    log "✅ RKMPP 编器可用"
else
    log "❌ 没有 RKMPP 编器,检查二进制"
    exit 1
fi

# ---------- 设备权限 ----------
log "把当前用户加进 video 组(RKMPP 设备访问需要)"
SERVICE_USER="${SUDO_USER:-kxrdyf}"
usermod -aG video "$SERVICE_USER" 2>/dev/null || true
log "已加 $SERVICE_USER 到 video 组(下次登录生效)"

# ---------- cron ----------
log "写 cron (每天 02:00)..."
CRON_FILE="/etc/cron.d/video-manager"
cat > "$CRON_FILE" <<EOF
# 每天 02:00 触发 video-manager 自动转码
# video-manager 会用 $INSTALL_PATH (jellyfin-ffmpeg RKMPP 硬编)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

0 2 * * * $SERVICE_USER /usr/bin/curl -fsS -X POST http://127.0.0.1:8765/api/run \\
    -H 'Content-Type: application/json' \\
    -d '{"trigger":"cron"}' \\
    >> /home/$SERVICE_USER/scripts/video-manager/logs/cron.log 2>&1
EOF
chmod 644 "$CRON_FILE"

if systemctl is-active --quiet cron 2>/dev/null; then
    systemctl restart cron
elif systemctl is-active --quiet crond 2>/dev/null; then
    systemctl restart crond
fi
log "✅ cron 装好"

# ---------- 总结 ----------
log ""
log "=== 下一步:验证 RKMPP 在跑 ==="
log "1. 重启 video-manager 让它重新探测 ffmpeg:"
log "   sudo systemctl restart video-manager"
log ""
log "2. 触发一次:"
log "   sudo -u $SERVICE_USER curl -X POST http://127.0.0.1:8765/api/run \\"
log "        -H 'Content-Type: application/json' -d '{\"trigger\":\"manual\"}'"
log ""
log "3. 看 logs/app.log 应该有:"
log "   加速: rkmpp  分辨率: 1280x720@10fps"
log ""
log "4. 第一次手动跑完后,改一下 video-manager/app.py 的 _build_ffmpeg_cmd:"
log "   rkmpp 分支用 vpp_rkrga 硬缩放(实测 12x 实时)"
log "   见 contrib/rkmpp-patch.md 或自己改"