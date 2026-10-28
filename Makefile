VENV := .venv
PY ?= $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
# 依赖源：默认清华镜像（国内快）。境外网络用 PIP_INDEX=https://pypi.org/simple make setup
PIP_INDEX ?= https://pypi.tuna.tsinghua.edu.cn/simple
PIP := $(VENV)/bin/python -m pip install -q -i $(PIP_INDEX) --timeout 15 --retries 2
export PYTHONPATH := src

.PHONY: setup test verify verify-release demo doctor tools audit-ui soak clean

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
verify-release:
	@$(PY) -c "import numpy" 2>/dev/null || { echo "❌ 缺 numpy —— 先跑 make setup"; exit 1; }
	$(PY) scripts/verify.py --release

demo:
	@$(PY) -c "import numpy" 2>/dev/null || { echo "❌ 缺 numpy —— 先跑 make setup"; exit 1; }
	$(PY) -m voxos.main demo

# 界面回归：把审查里量出来的数重新量一遍（需要先起 serve）
audit-ui:
	@curl -s --noproxy '*' -o /dev/null http://127.0.0.1:$${PORT:-8765}/ \
		|| { echo "❌ 先起服务：voxos serve"; exit 1; }
	$(PY) scripts/audit_ui.py --port $${PORT:-8765}

# 长稳：默认 30 分钟，覆盖不到真麦克风热插拔
soak:
	$(PY) -u scripts/soak.py --minutes $${MINUTES:-30} --stt $${STT:-sense_voice}

doctor:
	$(PY) -m voxos.main doctor

tools:
	$(PY) -m voxos.main tools

clean:
	rm -rf .pytest_cache **/__pycache__ .voxos_backup
