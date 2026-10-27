#!/bin/sh
# 把 VoxOS 装成登录自启的后台守护进程（macOS launchd）。
#
#   ./scripts/install_launchd.sh            # 安装并立即启动
#   ./scripts/install_launchd.sh --status   # 看是否在跑
#   ./scripts/install_launchd.sh --uninstall
#   ./scripts/install_launchd.sh --print    # 只打印 plist，不动系统
#
# 关于麦克风权限（重要，别跳过）：
# macOS 把麦克风权限绑在"申请它的那个可执行文件"上。launchd 起的进程没有窗口，
# 弹不出授权框，只会静默拿不到音频。所以**第一次必须先手动跑一遍**：
#   .venv/bin/voxos run
# 在弹出的系统对话框里点"允许"，再执行本脚本安装开机自启。

set -eu

LABEL="com.voxos.agent"
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/.voxos/logs"
UID_NUM=$(id -u)

PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=$(command -v python3)
CONFIG="$ROOT/config/settings.toml"

render() {
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>voxos.main</string>
    <string>--config</string>
    <string>$CONFIG</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key><string>$ROOT/src</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOGS/voxos.out.log</string>
  <key>StandardErrorPath</key><string>$LOGS/voxos.err.log</string>
</dict>
</plist>
PLIST
}

case "${1:-}" in
    --print)
        render
        exit 0
        ;;
    --status)
        launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | head -12 \
            || echo "未安装（$PLIST 不存在）"
        exit 0
        ;;
    --uninstall)
        launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
        rm -f "$PLIST"
        echo "已卸载：$LABEL"
        exit 0
        ;;
esac

mkdir -p "$LOGS" "$HOME/Library/LaunchAgents"
render > "$PLIST"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM/$LABEL" "$PLIST"
echo "已安装并启动：$LABEL"
echo "  配置：$CONFIG"
echo "  日志：$LOGS/voxos.out.log"
echo "  停止：launchctl bootout gui/$UID_NUM/$LABEL"
