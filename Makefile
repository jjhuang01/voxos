VENV := .venv
PY ?= $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
# 依赖源：默认清华镜像（国内快）。境外网络用 PIP_INDEX=https://pypi.org/simple make setup
PIP_INDEX ?= https://pypi.tuna.tsinghua.edu.cn/simple
PIP := $(VENV)/bin/python -m pip install -q -i $(PIP_INDEX) --timeout 15 --retries 2
export PYTHONPATH := src

.PHONY: setup test verify verify-release demo doctor tools audit-ui soak install-smoke clean

# 一条命令把可跑的仓库变成能跑的仓库：建 .venv 并装齐依赖
setup:
	python3 -m venv $(VENV)
	$(PIP) -U pip || echo "⚠️  pip 自更新失败，用自带版本继续"
	$(PIP) -e ".[dev,models,audio]"
	@echo "✅ 依赖就绪：$(VENV)/bin/python"

# 缺依赖时给一句人话，而不是 ModuleNotFoundError
# --basetemp 钉在仓库内：受限环境里 pytest 默认的 /var/folders 临时目录会被
# 文件代理拒掉（mkdir 已存在的目录 → PermissionError: EEXIST），90 个用例全废。
test:
	@$(PY) -c "import pytest" 2>/dev/null || { echo "❌ 缺 pytest —— 先跑 make setup"; exit 1; }
	$(PY) -m pytest -q --basetemp=.voxos/pytest-tmp

verify:
	@$(PY) -c "import numpy" 2>/dev/null || { echo "❌ 缺 numpy —— 先跑 make setup"; exit 1; }
	$(PY) scripts/verify.py

# 发布门禁：GAP / SKIP 不再放行。回归用 `make verify`，发版前必须过这条。
# GAP（中英混说 95%）在现有合成语料下关不掉——两套 TTS 语料都远不到，要真人录音。
# 所以走显式豁免：必须写理由和到期日，到期自动失效。豁免的意思是"知道并接受"，
# 不是"假装通过"：它仍然会被打印成 WARN，并算进验收报告的"未达标"里。
#
# 声纹 FAR 是第二条：阈值只有一个标量，合成语料的最优点在 0.80~0.84，
# 真麦克风要到 0.75 才不误拒本人——两者不可兼得。扫描表里存在 FRR=FAR=0 的点，
# 说明后端分得开，差的只是"一个阈值服务两种通道"，属于标定边界。
# 真修法是按通道标定（录声纹时一并记通道，或做 cohort 归一化），见 TODO_ROADMAP。
verify-release:
	@$(PY) -c "import numpy" 2>/dev/null || { echo "❌ 缺 numpy —— 先跑 make setup"; exit 1; }
	$(PY) scripts/verify.py --release \
		--allow-gap "中英混说" \
		--allow-gap "声纹 FAR" \
		--reason "中英混说需真人语料；声纹 FAR 是单阈值跨通道的标定边界，非能力缺陷" \
		--until 2027-03-31

demo:
	@$(PY) -c "import numpy" 2>/dev/null || { echo "❌ 缺 numpy —— 先跑 make setup"; exit 1; }
	$(PY) -m voxos.main demo

# 界面回归：把审查里量出来的数重新量一遍（需要先起 serve）
audit-ui:
	@curl -s --noproxy '*' -o /dev/null http://127.0.0.1:$${PORT:-8765}/ \
		|| { echo "❌ 先起服务：voxos serve"; exit 1; }
	$(PY) scripts/audit_ui.py --port $${PORT:-8765}

# 装一遍再验。源码布局里「资源根 == 数据根」，所以 0.1.11（相对路径按 cwd 解析）
# 和 0.1.12（配置写回只读的安装资源）在 `make test` 下永远是绿的——它们只在
# 真的装出来之后才现形。CI 的 package job 跑的是同一个脚本。
install-smoke:
	@$(PY) -c "import build" 2>/dev/null || { echo "❌ 缺 build —— 先跑 make setup"; exit 1; }
	$(PY) -m build
	bash scripts/install_smoke.sh dist/*.whl $(PY)

# 长稳：默认 30 分钟，覆盖不到真麦克风热插拔
soak:
	$(PY) -u scripts/soak.py --minutes $${MINUTES:-30} --stt $${STT:-sense_voice}

doctor:
	$(PY) -m voxos.main doctor

tools:
	$(PY) -m voxos.main tools

clean:
	rm -rf .pytest_cache **/__pycache__ .voxos_backup
