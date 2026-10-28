#!/bin/sh
# 在**干净上下文**里验证命令沙箱真的生效。
#
# 为什么需要这个：macOS 不允许嵌套收紧 seatbelt。如果当前 shell 本身已经在某个
# 沙箱里（IDE 的内置终端、CI runner、任何 `sandbox-exec` 包起来的东西），
# `sandbox-exec` 对任何比现状更严的 profile 都返回 EPERM——看起来像「profile 写错了」，
# 其实是环境不允许。而 `launchctl` 和 `open -a Terminal x.command` 都不行
# （沙箱上下文会继承），只有真正被 LaunchServices 拉起的 app bundle 是干净的。
#
# 用法：sh scripts/sandbox_probe.sh [仓库路径]
# 结果：/tmp/voxos-sandbox-probe.log

set -e

REPO=$(cd "${1:-$(dirname "$0")/..}" && pwd)
APP=/tmp/VoxosSandboxProbe.app
LOG=/tmp/voxos-sandbox-probe.log
PY=$REPO/.venv/bin/python
[ -x "$PY" ] || PY=$(command -v python3)

rm -rf "$APP" "$LOG"
mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>probe</string>
<key>CFBundleIdentifier</key><string>com.voxos.sandboxprobe</string>
<key>CFBundleName</key><string>VoxosSandboxProbe</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/probe" <<PROBE
#!/bin/sh
exec > $LOG 2>&1
P=$PY
R=$REPO
echo "### cwd=\$(pwd)"
PYTHONPATH=\$R/src \$P -c "
import sys; sys.path.insert(0,'\$R/src')
from voxos import sandbox
from voxos.config import load
print('status =', sandbox.status())
print('roots  =', ' '.join(str(one) for one in sandbox.writes_roots(load())))
print('   （上面是 cwd=/ 时的可写根：不含仓库才对，因为 / 不算边界）')
" 2>&1

run() {
  echo "### \$1"
  ( cd "\${2:-/}" && PYTHONPATH=\$R/src \$P -c "
import sys; sys.path.insert(0,'\$R/src')
from voxos import core, tools
from voxos.config import load
cfg = load(); tools.configure(cfg); core.ensure_tools()
ex = core.Executor(cfg=cfg, confirm=lambda *a: True)
print(ex.execute_safely('run_command', {'command': '''\$1'''}))
" 2>&1 )
}

# 同一路径、只差 cwd：证明「启动目录进可写根，但 / 不进」
run 'touch _voxos_probe.txt && echo repo-write-ok' "\$R"
run "touch \$R/_voxos_probe.txt && echo should-not-happen"
# /dev 只开必要节点
run 'echo hi > /dev/null && echo devnull-ok'
# 家目录其它地方必须被拒
run 'echo x > ~/Desktop/_voxos_probe_fail.txt && echo should-not-happen'
# 临时目录必须可写（git/npm/编译器的中间文件都在这儿）
run 'echo t > "\$TMPDIR/_voxos_probe.txt" && echo tmp-ok'
# 默认断网
run 'curl -sS -m 5 https://example.com > /dev/null && echo net-ok || echo net-blocked'

echo "== done =="
PROBE

chmod +x "$APP/Contents/MacOS/probe"
open -a "$APP"

# 等日志写完。探针自己很快，但 LaunchServices 拉起有延迟。
i=0
while [ $i -lt 40 ]; do
  if grep -q "== done ==" "$LOG" 2>/dev/null; then break; fi
  i=$((i + 1))
  sleep 0.5
done

cat "$LOG"
echo
echo "--- 期望：repo-write-ok / 两次 should-not-happen 都不出现 / devnull-ok / tmp-ok / net-blocked"
rm -f "$REPO/_voxos_probe.txt" "$HOME/Desktop/_voxos_probe_fail.txt"
rm -rf "$APP"
