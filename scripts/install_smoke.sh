#!/usr/bin/env bash
# 装完才验得出来的东西：wheel → 干净 venv → 任意工作目录 → doctor / 配置写回。
#
# 为什么单开一个脚本：0.1.11（STT 相对路径按当前工作目录解析）和 0.1.12
# （设置写回只读的安装资源）都是「源码跑正常、装完就坏」。pytest 跑在源码布局里，
# 资源根 == 数据根，这两类问题永远不会暴露。这个脚本用临时 HOME + 临时 venv
# 造出真实安装布局，CI 和本地跑的是同一份，不靠人肉再装一遍才发现。
#
# 用法: scripts/install_smoke.sh dist/voxos-0.1.13-py3-none-any.whl [python3]
#
# 装依赖（不加 --no-deps）：numpy 是 `dependencies` 里的硬依赖，`audio.py` 在模块
# 顶层就 import 它，而 `main.py` 又导入 audio——所以 `--no-deps` 装出来的命令
# 连 `--version` 都打不出来（实测 ModuleNotFoundError）。这也顺带说明
# `cmd_doctor` 里「numpy 缺失」那一支其实是不可达的，见 TODO_ROADMAP 的已知缺口。
set -euo pipefail

WHEEL="${1:?用法: install_smoke.sh <wheel> [python]}"
PYTHON="${2:-python3}"
[ -f "$WHEEL" ] || { echo "❌ 找不到 wheel：$WHEEL"; exit 1; }
WHEEL="$(cd "$(dirname "$WHEEL")" && pwd)/$(basename "$WHEEL")"

# 必须在建 venv 之前清掉：`make` 会 export PYTHONPATH=src，而仓库里还留着
# 可编辑安装生成的 `src/voxos.egg-info`。pip 顺着 PYTHONPATH 看见那个 egg-info，
# 就认为「voxos 已经装了同样的版本」，于是**静默跳过 wheel 安装**——venv 里
# 没有 voxos 命令，报的却是 `No such file or directory`，看上去像路径写错了。
unset PYTHONPATH 2>/dev/null || true

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
VENV="$WORK/venv"
HOME_DIR="$WORK/home"
ELSEWHERE="$WORK/elsewhere"
mkdir -p "$HOME_DIR" "$ELSEWHERE"

step() { printf '▸ %s\n' "$1"; }
die() { printf '❌ %s\n' "$1"; exit 1; }

step "装进干净 venv：$VENV"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install -q "$WHEEL"

# HOME 和 VOXOS_HOME 一起钉：前者走 Path.home()，后者是显式覆盖。
# 两个都设，任何一个生效都不会碰真实的 ~/.voxos。
export HOME="$HOME_DIR"
export VOXOS_HOME="$HOME_DIR"
cd "$ELSEWHERE"          # 关键：不是仓库根，也不是安装目录

step "1/7 --version"
WANT="voxos $(basename "$WHEEL" | sed -E 's/^voxos-([^-]+)-.*/\1/')"
GOT="$("$VENV/bin/voxos" --version)"
[ "$GOT" = "$WANT" ] || die "--version 期望「${WANT}」，实际「${GOT}」"
printf '  %s\n' "$GOT"

step "2/7 doctor 能跑，且说得清配置在哪"
"$VENV/bin/voxos" doctor > "$WORK/doctor.txt" || {
    cat "$WORK/doctor.txt"; die "doctor 退出码非 0（装完第一件事就跑不起来）"
}
grep -q "声纹模型" "$WORK/doctor.txt" || { cat "$WORK/doctor.txt"; die "doctor 没有声纹模型行"; }
grep -q "配置文件" "$WORK/doctor.txt" || { cat "$WORK/doctor.txt"; die "doctor 没报配置路径"; }
grep -q "数据根" "$WORK/doctor.txt" || { cat "$WORK/doctor.txt"; die "doctor 没报数据根"; }

step "3/7 首次启动 seed 出用户配置（0.1.12 的坑）"
SEEDED="$HOME_DIR/.voxos/settings.toml"
# 变量后面紧跟全角字符时必须写 ${VAR}：某些 locale 下 bash 会把全角字节
# 当成标识符的一部分，报一个「SEEDED<乱码>: unbound variable」——
# 而且只有走到报错分支才会暴露，成功路径永远看不见。
[ -f "$SEEDED" ] || die "没 seed 出 ${SEEDED}（装完改不了配置）"
grep -q "只归你" "$SEEDED" || die "seed 出来的配置没带说明注释"
printf '  %s\n' "$SEEDED"

step "4/7 资源根与数据根分家"
"$VENV/bin/python" - "$VENV" "$HOME_DIR" <<'PY' || die "资源根/数据根不对"
import sys
from pathlib import Path
from voxos import config

venv, home = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
root, data = Path(config.ROOT).resolve(), Path(config.DATA).resolve()
assert root == venv / "share" / "voxos", f"资源根应在安装目录，实际 {root}"
assert data == home, f"数据根应在家目录，实际 {data}"
assert venv not in data.parents, "数据根落进了安装目录（pipx upgrade 会一起删）"
print(f"  资源根 {root}")
print(f"  数据根 {data}")
PY

step "5/7 make_stt 的相对 model_dir 按数据根解析（0.1.11 的坑）"
# 必须真的调 make_stt：0.1.11 的 bug 在 `make_stt` 里用 `cfg.get` 取了相对路径，
# 相对的是**当前工作目录**。只验 `cfg.path()` 会绿——那是 config.py 的行为，
# 而坏掉的是调用方。这里把 SenseVoice 后端换成一个只记参数的替身。
"$VENV/bin/python" - "$HOME_DIR" "$ELSEWHERE" <<'PY' || die "make_stt 解析错了"
import sys
from pathlib import Path
from voxos import audio
from voxos.config import load

seen = []

class Recorder:
    def __init__(self, model_dir, num_threads=2):
        seen.append(Path(model_dir))

audio.SenseVoiceStt = Recorder
cfg = load().with_overrides(**{"stt.backend": "sense_voice"})
audio.make_stt(cfg)
assert seen, "make_stt 没走到 sense_voice 分支"
resolved = seen[0].resolve()
home, elsewhere = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
assert home in resolved.parents, f"model_dir 没落在数据根：{resolved}"
assert elsewhere not in resolved.parents, f"model_dir 被当前工作目录带偏了：{resolved}"
print(f"  {resolved}")
PY

step "6/7 改配置 → 重启还在，安装目录那份一个字节没动"
BEFORE="$(shasum -a 256 "$VENV/share/voxos/config/settings.toml" | cut -d' ' -f1)"
"$VENV/bin/python" - <<'PY' || die "配置写回失败"
from voxos import settings
from voxos.config import load

cfg, errors = settings.apply(load(), {"stt.backend": "sense_voice"})
assert not errors, errors
assert load().get("stt.backend") == "sense_voice", "写进去了但读不回来"
PY
AFTER="$(shasum -a 256 "$VENV/share/voxos/config/settings.toml" | cut -d' ' -f1)"
[ "$BEFORE" = "$AFTER" ] || die "安装目录那份配置被改动了（升级会整份替换，等于白改）"
grep -q 'backend = "sense_voice"' "$SEEDED" || die "改动没落到用户配置里"

step "7/7 启动 voxos run（独立临时端口/临时数据根），等端口监听后精确停掉"
RUN_DIR="$WORK/run_test"
mkdir -p "$RUN_DIR"
FREE_PORT="$("$VENV/bin/python" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
[ -n "$FREE_PORT" ] || die "获取临时空闲端口失败"

# 在临时配置下写入 ui.port 配置，避免撞 8765/8766
"$VENV/bin/python" - "$FREE_PORT" <<'PY' || die "写入临时端口配置失败"
import sys
from voxos import settings
from voxos.config import load

settings.apply(load(), {"ui.port": int(sys.argv[1])})
PY

# 启动 voxos run
"$VENV/bin/voxos" run > "$RUN_DIR/voxos.log" 2>&1 &
RUN_PID=$!

# 等待端口可连接（10 秒超时）
"$VENV/bin/python" - "$FREE_PORT" "$RUN_PID" <<'PY' || {
import socket, sys, time

port = int(sys.argv[1])
pid = int(sys.argv[2])
deadline = time.time() + 10.0
connected = False
while time.time() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            connected = True
            break
    except OSError:
        time.sleep(0.1)

if not connected:
    sys.exit(1)
PY
    cat "$RUN_DIR/voxos.log"
    kill -9 "$RUN_PID" 2>/dev/null || true
    die "voxos run 在 10 秒内未监听端口 $FREE_PORT"
}

# 精确停掉启动的那个 pid
kill -TERM "$RUN_PID" 2>/dev/null || true
wait "$RUN_PID" 2>/dev/null || true
if kill -0 "$RUN_PID" 2>/dev/null; then
    kill -9 "$RUN_PID" 2>/dev/null || true
    die "voxos run (pid $RUN_PID) 未能在收到 SIGTERM 后退出"
fi
printf '  成功启动并监听 127.0.0.1:%s，已精确终止 pid %s\n' "$FREE_PORT" "$RUN_PID"

printf '\n✅ 安装冒烟 7/7 通过（%s）\n' "$(basename "$WHEEL")"
