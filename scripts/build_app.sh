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
  <!-- WKWebView 默认按 ATS 拒绝 http://，而界面就在 http://127.0.0.1 上。
       少了这条：窗口起得来、但永远白屏——最难查的那种失败 -->
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSMicrophoneUsageDescription</key>
  <string>VoxOS 需要麦克风来听你说话：声纹比对与语音识别都在本机完成，音频不上传。</string>
</dict>
</plist>
PLIST

# ---- 2.5 原生胶囊窗口：Swift + WKWebView ---------------------------------
# 为什么不用 Chrome 的 --app：冷启动 1.2–1.5s、常驻 300MB+，而这两件事正是
# 「打开半天打不开」和「内存高」的主因。原生窗口 ~0.32s、~60MB（实测见
# docs/PERFORMANCE.md §1）。没有 swiftc 就跳过，启动器会自动退回 Chrome。
step "编译原生窗口（Swift / WKWebView）"
if command -v swiftc >/dev/null 2>&1; then
    swiftc -O -whole-module-optimization -o "$BUILD/Contents/MacOS/VoxOSWindow" \
        "$ROOT/native/VoxOSWindow.swift" \
        -framework Cocoa -framework WebKit \
        -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist \
        -Xlinker "$ROOT/native/ats.plist" \
        || die "Swift 编译失败（没有命令行工具？xcode-select --install）"
else
    warn "没有 swiftc，跳过原生窗口：启动器会退回 Chrome（慢 1 秒、多 300MB）"
fi

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
#
# 为什么不用「voxos port」那条命令：它要走一次完整的包导入（numpy / sherpa /
# sounddevice），光为了取一个整数就多花一次解释器冷启动。双击到看见窗口本来
# 就只有两三秒，这种开销全在用户感知里。sed 只认 [ui] 段里那行 port，读不到退回 8765。
# （注释里也不能写反引号：这段是未加引号的 heredoc，反引号会被当成命令替换执行。）
read_port() {
    local cfg="\$HOME/.voxos/settings.toml" value=""
    [ -f "\$cfg" ] && value=\$(sed -n '/^\[ui\]/,/^\[/p' "\$cfg" \\
        | sed -n 's/^[[:space:]]*port[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)
    echo "\${value:-8765}"
}
PORT="\${VOXOS_PORT:-\$(read_port)}"
LOG="\$HOME/.voxos/logs/app.log"
mkdir -p "\$(dirname "\$LOG")"

alive() { curl -fsS --max-time 1 "http://127.0.0.1:\$PORT/" 2>/dev/null | grep -q "$APP_NAME"; }

# 原生窗口就在同目录（Contents/MacOS/VoxOSWindow）。没有它才退回 Chrome：
# 源码布局 / 老 bundle 里可能还没有这个二进制。
DIR="\$(cd "\$(dirname "\$0")" && pwd)"
WINDOW="\$DIR/VoxOSWindow"
WINDOW_PID=""

open_window() {
    if [ -x "\$WINDOW" ]; then
        "\$WINDOW" --url "http://127.0.0.1:\$PORT/" >/dev/null 2>&1 &
        WINDOW_PID=\$!
    else
        "\$BIN" pill
    fi
}

window_running() {
    [ -n "\$WINDOW" ] && [ -x "\$WINDOW" ] && pgrep -f "\$WINDOW" >/dev/null 2>&1
}

if alive; then
    if window_running; then
        # 服务和窗口都在（窗口可能被别的窗口挡住）。再开一个会出现两个胶囊
        # 抢同一条状态流——用户看到的是「点了没反应 + 两个一样的窗口」。
        exit 0
    fi
    # 服务在跑但窗口没了（用户关了窗口，守护进程被 ⌘Q 之外的路径留下来了）：
    # 双击 = 开个窗口，0.3 秒的事。
    open_window
    [ -n "\$WINDOW_PID" ] && wait "\$WINDOW_PID"
    exit 0
fi

# 服务没在跑，窗口却还在：那是一个**孤儿窗口**，页面连的是一个已经死掉的服务。
# 以前这里直接 exit 0，症状是「双击 App 什么都不发生」——用户没法自救，
# 因为窗口可能被别的窗口挡住，看起来就是没反应。现在清掉它，重新起。
if window_running; then
    pkill -f "\$WINDOW" 2>/dev/null
    sleep 0.3
fi

echo "=== \$(date '+%F %T') 启动 VoxOS ===" >>"\$LOG"
# PYTHONUNBUFFERED：stdout 重定向到文件时 Python 会改成块缓冲，启动横幅
# （含「命令沙箱=… 生效/不生效」那行）会卡在缓冲区里，进程活多久就憋多久——
# 日志看着像「起来了但什么都没说」，其实说了，只是还没吐出来。
# 同一件事也让 traceback 立即落盘：App 没终端，日志是唯一能事后看的地方。
PYTHONUNBUFFERED=1 "\$BIN" run >>"\$LOG" 2>&1 &
CHILD=\$!
trap 'kill "\$CHILD" \${WINDOW_PID:-} 2>/dev/null' EXIT INT TERM

# 窗口先出来，内容后到：原生窗口自己会重试连服务，并先画一行「正在启动…」。
# 用户感知的是「点了就有东西出现」，不是「数据到了」——这两件事差 0.5 秒，
# 而 0.5 秒就是「秒开」和「半天打不开」的分界。
open_window

# 100ms 一探：服务本身 0.35s 就绪，250ms 的步长等于白等最多 0.25s
for _ in \$(seq 1 200); do
    alive && break
    kill -0 "\$CHILD" 2>/dev/null || break
    sleep 0.1
done

if ! alive; then
    # App 没有终端，失败必须弹出来，否则用户只看到图标跳一下就没了。
    # 弹窗里要带上**最后一行错误**：只说「日志在 ~/.voxos/logs/app.log」等于
    # 让用户自己去找，而他此刻还不知道「日志」是什么东西。
    WHY=\$(grep -E '^(RuntimeError|ModuleNotFoundError|PermissionError|OSError|ImportError)' "\$LOG" | tail -1)
    [ -n "\$WHY" ] || WHY=\$(tail -1 "\$LOG")
    osascript -e "display alert \\"VoxOS 没能启动\\" message \\"\${WHY:-未知原因}（日志：~/.voxos/logs/app.log）\\" as critical" >/dev/null 2>&1
    exit 1
fi

# 等**任意一个**先死，然后把另一个带走。
#
# 原来写的是「只 wait 窗口」：守护进程先死的时候，启动器
# 一动不动，窗口还挂着一个已经死掉的页面。更糟的是 App 的可执行文件就是这个
# 启动器，LaunchServices 认为它还在运行，所以**再双击只会「激活」它，不会起新的**
# ——用户看到的就是「点了没反应」，而且没法自救。
#
# 不能用 wait -n：macOS 自带的是 bash 3.2，没有这个选项。
while kill -0 "\$CHILD" 2>/dev/null \
      && { [ -z "\$WINDOW_PID" ] || kill -0 "\$WINDOW_PID" 2>/dev/null; }; do
    sleep 0.5
done
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
