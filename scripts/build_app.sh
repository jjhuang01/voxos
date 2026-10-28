#!/usr/bin/env bash
# 把 VoxOS 打成 macOS 应用（VoxOS.app），可选直接装进 /Applications。
#
#   bash scripts/build_app.sh              # 只构建到 build/VoxOS.app
#   bash scripts/build_app.sh --install    # 构建并装进 /Applications
#   bash scripts/build_app.sh --uninstall  # 从 /Applications 删掉
#
# 为什么需要一个 .app 而不是「一条 pipx 命令」：
# 1. 双击就能用——不用记住命令、不用开终端；
# 2. **麦克风权限认 bundle**。macOS 把 TCC 授权绑在「谁在申请」上，命令行里
#    第一次跑会弹窗、之后跟着终端走；做成 App 后，权限属于 VoxOS 自己，
#    系统设置 → 隐私与安全性 → 麦克风 里能看到「VoxOS」这一项，
#    用户想关就关、想开就开。没有 NSMicrophoneUsageDescription 的 App
#    一旦访问麦克风会被系统直接杀掉。
# 3. 退出有入口：App 前台守着守护进程，退出即带走，不会留一个看不见的僵尸。
set -euo pipefail

APP_NAME="VoxOS"
BUNDLE_ID="com.voxos.agent"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build/$APP_NAME.app"
TARGET="/Applications/$APP_NAME.app"

step() { printf '▸ %s\n' "$1"; }
die() { printf '❌ %s\n' "$1"; exit 1; }

case "${1:-}" in
    --uninstall)
        [ -d "$TARGET" ] || { echo "没装（${TARGET} 不存在）"; exit 0; }
        rm -rf "$TARGET"
        echo "已删除 $TARGET"
        exit 0
        ;;
    --install|"") ;;
    *) die "不认识的参数：$1（可用：--install / --uninstall）" ;;
esac

# ---- 1. 找到真实的 voxos 可执行文件（pipx 装的那个）----------------------
VX="$(command -v voxos || true)"
[ -n "$VX" ] || die "找不到 voxos 命令，先跑 scripts/install.sh"
VX="$(cd "$(dirname "$VX")" && pwd)/$(basename "$VX")"
VERSION="$("$VX" --version | awk '{print $2}')"
step "可执行文件：${VX}（v${VERSION}）"

# ---- 2. 组装 bundle -----------------------------------------------------
rm -rf "$BUILD"
mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources"
step "组装 bundle：$BUILD"

cat > "$BUILD/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIconFile</key><string>$APP_NAME</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><false/>
  <key>NSHighResolutionCapable</key><true/>
  <!-- 没有这一条，App 一碰麦克风就被系统杀掉，而且不给任何提示 -->
  <key>NSMicrophoneUsageDescription</key>
  <string>VoxOS 需要麦克风来听你说话：声纹比对与语音识别都在本机完成，音频不上传。</string>
</dict>
</plist>
PLIST

# ---- 3. 启动器：起守护进程 → 开胶囊 → 前台守着 -------------------------
cat > "$BUILD/Contents/MacOS/$APP_NAME" <<LAUNCHER
#!/bin/bash
# 双击后：没在跑就起 run，等端口通了再开胶囊窗口；退出时把守护进程一起带走。
set -u
# Finder 启动的进程只有最小 PATH，tmux/say 都在 /opt/homebrew 或 /usr/local，
# 不补这一句，App 里「说一句话」会静默少一半能力。
export PATH="\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
BIN="$VX"
# 端口从配置里读，不写死：8765 被别的程序占用时，写死会让启动器永远等不到服务就绪，
# 然后弹一个「没能启动」的框，而真实原因（Address already in use）只在日志里。
PORT="\${VOXOS_PORT:-\$("\$BIN" port 2>/dev/null || echo 8765)}"
LOG="\$HOME/.voxos/logs/app.log"
mkdir -p "\$(dirname "\$LOG")"

alive() { curl -fsS --max-time 1 "http://127.0.0.1:\$PORT/" 2>/dev/null | grep -q "$APP_NAME"; }

if alive; then
    "\$BIN" pill
    exit 0
fi

echo "=== \$(date '+%F %T') 启动 VoxOS ===" >>"\$LOG"
"\$BIN" run >>"\$LOG" 2>&1 &
CHILD=\$!
trap 'kill "\$CHILD" 2>/dev/null' EXIT INT TERM

for _ in \$(seq 1 80); do
    alive && break
    kill -0 "\$CHILD" 2>/dev/null || break
    sleep 0.25
done

if ! alive; then
    # App 没有终端，失败必须弹出来，否则用户只看到图标跳一下就没了
    osascript -e 'display alert "VoxOS 没能启动" message "日志：~/.voxos/logs/app.log" as critical' >/dev/null 2>&1
    tail -5 "\$LOG" >/dev/null 2>&1
    exit 1
fi

"\$BIN" pill
wait "\$CHILD"
LAUNCHER
chmod +x "$BUILD/Contents/MacOS/$APP_NAME"

# ---- 4. 图标 ------------------------------------------------------------
step "生成图标"
"$ROOT/.venv/bin/python" "$ROOT/scripts/make_icon.py" "$ROOT/build/icon.png" >/dev/null 2>&1 \
    || python3 "$ROOT/scripts/make_icon.py" "$ROOT/build/icon.png" >/dev/null
ICONSET="$ROOT/build/$APP_NAME.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
# iconutil 只认这十个固定文件名（1x/2x 成对），缺一个就不出 icns
for pair in "16:icon_16x16" "32:icon_16x16@2x" "32:icon_32x32" "64:icon_32x32@2x" \
            "128:icon_128x128" "256:icon_128x128@2x" "256:icon_256x256" \
            "512:icon_256x256@2x" "512:icon_512x512" "1024:icon_512x512@2x"; do
    size="${pair%%:*}"; name="${pair#*:}"
    sips -z "$size" "$size" "$ROOT/build/icon.png" --out "$ICONSET/$name.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD/Contents/Resources/$APP_NAME.icns"

# ---- 5. 签名（ad-hoc）：TCC 靠它把「麦克风权限」记在这个 App 上 ----------
step "ad-hoc 签名"
codesign --force --deep --sign - "$BUILD" >/dev/null 2>&1 \
    || echo "⚠️  签名失败（不影响本机使用，但每次启动可能重新询问麦克风权限）"
plutil -lint "$BUILD/Contents/Info.plist" >/dev/null || die "Info.plist 不合法"

echo
echo "✅ 已构建：$BUILD"
if [ "${1:-}" = "--install" ]; then
    step "装进 /Applications"
    # 覆盖安装前先把旧的退出，否则 launchd 还指着旧 bundle
    osascript -e "tell application \"$APP_NAME\" to quit" >/dev/null 2>&1 || true
    rm -rf "$TARGET"
    ditto "$BUILD" "$TARGET"          # 比 cp -R 更忠实于 bundle 的元数据
    echo "✅ 已安装：$TARGET"
    echo
    echo "打开方式：Finder → 应用程序 → ${APP_NAME}（或 open -a ${APP_NAME}）"
    echo "第一次会弹麦克风授权，点「允许」——系统设置 → 隐私与安全性 → 麦克风 里能看到 ${APP_NAME}"
    echo "退出：⌘Q，或 voxos stop"
else
    echo "装进 /Applications：bash scripts/build_app.sh --install"
fi
