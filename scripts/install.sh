#!/usr/bin/env bash
# 一条命令装好 / 升级 VoxOS。
#
#   bash scripts/install.sh                        # 装 GitHub 上最新的 Release
#   bash scripts/install.sh dist/voxos-*.whl       # 装本地构建的包
#   bash scripts/install.sh --no-audio             # 只要核心，跳过音频依赖
#   curl -fsSL .../main/scripts/install.sh | bash  # 连仓库都不用 clone
#
# 为什么不是「README 里写三条命令，让用户自己按顺序敲」：那三条里第二条
# （`pipx inject`）是**升级时最容易漏**的一步——重装会重建 venv，把 inject 进去的
# sherpa-onnx / sounddevice 一起清掉。漏了之后 `voxos doctor` 只会说「降级」，
# 不会说「你上次注入的依赖没了」，于是模型下好了却永远跑脚本回放。
# 把顺序写进脚本，比写在文档里靠得住。
set -euo pipefail

REPO="jjhuang01/voxos"
WITH_AUDIO=1
SOURCE=""

usage() {
    # 不用 `sed -n '2,9p' "$0"`：`curl ... | bash` 时 $0 是 "bash"，读不到自己。
    cat <<'TEXT'
用法: install.sh [wheel 路径] [--no-audio] [--repo owner/name]

  不带参数      装 GitHub 上最新的 Release
  <wheel 路径>  装本地构建的包
  --no-audio    跳过 sherpa-onnx / sounddevice（核心链路仍可用，语音降级为脚本回放）
  --repo        换仓库（默认 jjhuang01/voxos）
TEXT
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-audio) WITH_AUDIO=0 ;;
        --repo) REPO="${2:?--repo 后面要跟 owner/name}"; shift ;;
        -h|--help) usage 0 ;;
        -*) echo "❌ 不认识的参数：$1"; usage 1 ;;
        *) SOURCE="$1" ;;
    esac
    shift
done

step() { printf '▸ %s\n' "$1"; }
warn() { printf '⚠️  %s\n' "$1"; }
die()  { printf '❌ %s\n' "$1"; exit 1; }

# 1. Python：pipx 会给包建独立 venv，但 venv 里的解释器得先够版本。
step "1/5 检查 Python"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "找不到 python3。macOS 用 brew install python@3.12，Debian 用 apt install python3"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "需要 Python ≥ 3.11，当前是 $("$PY" -V 2>&1)"
printf '  %s\n' "$("$PY" -V 2>&1)"

# 2. pipx：用它而不是 pip install，是为了不往系统环境里塞东西，
#    也让 `voxos` 命令自带一个可以整份删掉的 venv。
step "2/5 检查 pipx"
if ! command -v pipx >/dev/null 2>&1; then
    die "没装 pipx。macOS: brew install pipx && pipx ensurepath ｜ 其它: python3 -m pip install --user pipx"
fi
printf '  pipx %s\n' "$(pipx --version 2>&1 | head -1)"

# 3. 取包：给了本地路径就用它，否则去 Release 拿。
step "3/5 取安装包"
WORK=""
# `return 0` 不是多余的：EXIT trap 里最后一条命令的退出码会覆盖脚本原本的退出码，
# 而本地 wheel 路径下 $WORK 是空的，`[ -n "$WORK" ]` 返回 1——装成功了却报失败。
cleanup() { [ -z "$WORK" ] || rm -rf "$WORK"; return 0; }
trap cleanup EXIT

if [ -n "$SOURCE" ]; then
    [ -f "$SOURCE" ] || die "找不到 $SOURCE"
    WHEEL="$(cd "$(dirname "$SOURCE")" && pwd)/$(basename "$SOURCE")"
    printf '  本地：%s\n' "$WHEEL"
else
    WORK="$(mktemp -d)"
    if command -v gh >/dev/null 2>&1; then
        gh release download --repo "$REPO" --pattern '*.whl' -D "$WORK" --clobber >/dev/null
    else
        # 没有 gh 时走公开 API：release 里的浏览器下载地址就是 browser_download_url，
        # 不需要 token（仓库是公开的）。
        url="$("$PY" - "$REPO" <<'PY'
import json, sys, urllib.request
api = f"https://api.github.com/repos/{sys.argv[1]}/releases/latest"
with urllib.request.urlopen(api, timeout=30) as resp:
    data = json.load(resp)
print(next(a["browser_download_url"] for a in data["assets"] if a["name"].endswith(".whl")))
PY
)"
        curl -fsSL "$url" -o "$WORK/voxos.whl"
    fi
    WHEEL="$(ls "$WORK"/*.whl | head -1)"
    printf '  Release：%s\n' "$(basename "$WHEEL")"
fi

# 4. 装。这里**不用** `pipx install --force`：pipx 1.15 走 uv 后端时，force 重建 venv
#    会撞上 `error: A virtual environment already exists at: .`，然后 pipx 拒绝删除
#    「不是本次会话创建的」venv，于是升级在第二步就断了。实测可复现。
#    先 uninstall 再 install 是等价的，而且对老版本 pipx 也成立。
#    uninstall 只删 venv，~/.voxos 里的模型/声纹/日志/配置一个都不动。
step "4/5 安装（先卸后装，绕开 pipx 的 force 重建 bug）"
pipx uninstall voxos >/dev/null 2>&1 || true
pipx install "$WHEEL" >/dev/null || die "pipx 安装失败，加 -v 看详情：pipx install -v $WHEEL"

if [ "$WITH_AUDIO" = 1 ]; then
    step "5/5 音频依赖（sherpa-onnx + sounddevice）"
    # 装了就会在下次 `install --force` 时被清掉，所以每次安装都重放一遍；
    # --force 让重复执行不报「已经装了」。
    if ! pipx inject --force voxos sherpa-onnx sounddevice >/dev/null 2>&1; then
        warn "音频依赖没装上（可能没网或源不通）。核心链路仍可用，语音会降级成脚本回放。"
        warn "手动补：pipx inject voxos sherpa-onnx sounddevice"
    fi
else
    step "5/5 跳过音频依赖（--no-audio）"
fi

# 刚装完时 ~/.local/bin 可能还不在当前 shell 的 PATH 里（第一次装 pipx 的常见情况），
# 所以不去赌 `voxos` 能被找到，直接问 pipx 要 venv 路径。
BIN="$(pipx environment --value PIPX_LOCAL_VENVS)/voxos/bin/voxos"
[ -x "$BIN" ] || die "装完了但找不到可执行文件：$BIN"

printf '\n'
"$BIN" --version
if ! "$BIN" doctor; then
    warn "doctor 有异常项（上面逐行写了），按提示补，不影响已装好的命令。"
fi

cat <<EOF

下一步
  voxos models --no-asr     # 28MB：声纹 + VAD，够跑核心链路
  voxos models              # 267MB：再加 SenseVoice，本地真识别
  voxos enroll --record 3   # 录 3 遍声纹
  voxos run                 # 守护进程 + 胶囊

升级：重跑这条命令即可（依赖会跟着重放，不会丢）
EOF
