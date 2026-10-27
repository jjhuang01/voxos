VENV := .venv
PY ?= $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
# 依赖源：默认清华镜像（国内快）。境外网络用 PIP_INDEX=https://pypi.org/simple make setup
PIP_INDEX ?= https://pypi.tuna.tsinghua.edu.cn/simple
PIP := $(VENV)/bin/python -m pip install -q -i $(PIP_INDEX) --timeout 15 --retries 2
export PYTHONPATH := src

.PHONY: setup test verify demo doctor tools clean

# 一条命令把可跑的仓库变成能跑的仓库：建 .venv 并装齐依赖
setup:
	python3 -m venv $(VENV)
	$(PIP) -U pip || echo "⚠️  pip 自更新失败，用自带版本继续"
	$(PIP) -e ".[dev,models,audio]"
	@echo "✅ 依赖就绪：$(VENV)/bin/python"

# 缺依赖时给一句人话，而不是 ModuleNotFoundError
test:
	@$(PY) -c "import pytest" 2>/dev/null || { echo "❌ 缺 pytest —— 先跑 make setup"; exit 1; }
	$(PY) -m pytest -q

verify:
	@$(PY) -c "import numpy" 2>/dev/null || { echo "❌ 缺 numpy —— 先跑 make setup"; exit 1; }
	$(PY) scripts/verify.py

demo:
	@$(PY) -c "import numpy" 2>/dev/null || { echo "❌ 缺 numpy —— 先跑 make setup"; exit 1; }
	$(PY) -m voxos.main demo

doctor:
	$(PY) -m voxos.main doctor

tools:
	$(PY) -m voxos.main tools

clean:
	rm -rf .pytest_cache **/__pycache__ .voxos_backup
