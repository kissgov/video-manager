#!/usr/bin/env bash
# 给 install.sh 配套的 sudoers 一次性设置
# 跑法: sudo bash contrib/install-sudoers.sh
# 或:    sudo bash contrib/install.sh (会自动调这个)
#
# 作用: 给当前用户免密 systemctl 操作 video-manager 的权限
#       替代手写 /etc/sudoers.d/* 文件
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "需要 root,请用: sudo bash $0" >&2
  exit 1
fi

# 当前调用 sudo 的用户 (sudo -e 不可用时回退 SUDO_USER / logname)
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo kxrdyf)}"

RULES_FILE="/etc/sudoers.d/video-manager"
cat > "$RULES_FILE" <<EOF
# video-manager install/manage 自动配置 — 由 contrib/install-sudoers.sh 生成
# 允许 $TARGET_USER 免密操作 video-manager 服务和 reload systemd
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart video-manager
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl status video-manager
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl is-active video-manager
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now video-manager
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now video-manager
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start video-manager
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl stop video-manager
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl reload video-manager
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl daemon-reload
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/systemctl edit video-manager
$TARGET_USER ALL=(root) NOPASSWD: /bin/mv /tmp/video-manager.service.* /etc/systemd/system/video-manager.service
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/install -m 0644 /tmp/video-manager.* /etc/systemd/system/
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/tee /etc/systemd/system/video-manager.service
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/tailscale funnel *
$TARGET_USER ALL=(root) NOPASSWD: /usr/bin/tailscale serve *
EOF

chmod 0440 "$RULES_FILE"
# 验证
visudo -c -f "$RULES_FILE"
echo "✓ sudoers 规则已写入 $RULES_FILE (用户: $TARGET_USER)"
echo "  现在 $TARGET_USER 跑 install.sh 不用再输 sudo 密码"
