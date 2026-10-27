# AGENTS.md — AI 编程助手的最高宪章与上下文导航

> 任何 AI 编码工具（Cursor / Claude Code / Codex / Windsurf / WorkBuddy）在改动本仓库前，
> **必须先读完本文件**，再按任务类型读取对应规范。规范与代码冲突时，以规范为准并提 PR 修代码。

## 1. 项目定位

**VoxOS** 是一个本地优先的语音操作系统 Agent：只听你一个人说话（本地声纹锁），
一句话就能写结构化日志、续接编程 Session、只读巡检服务器，并通过极简悬浮胶囊反馈状态。

它不是聊天机器人。**没有主窗口、没有聊天记录页**。它的形态是：一个后台守护进程 + 一个 36px 胶囊。

## 2. 文档导航（改代码前必读）

| 你想做的事 | 先读 |
| :--- | :--- |
| 理解产品边界、用户旅程、验收口径 | `PRD.md` |
| 理解分层、数据流、目录职责 | `README.md` |
| 写任何一行 Python 之前 | `CODING_PHILOSOPHY.md`（最高代码审美标准） |
| 新增/修改工具、写正则快通道、改 Prompt | `PROMPTS_AND_ROUTING.md` |
| 新增工具、改风险等级、碰文件系统 | `REDLINES.md`（安全红线，不可协商） |
| 改 UI、音效、配色、动效 | `DESIGN_SYSTEM.md` |
| 决定"现在该做什么" | `TODO_ROADMAP.md` |
| 商业化、定价、指标 | `BUSINESS.md` |

## 3. 给代码生成模型的死命令（Coding Iron Rules）

1. **禁止省略**：不允许出现 `# TODO`、`pass`、`...省略其余代码...`、`NotImplementedError` 占位。
   每个函数必须可直接运行。
2. **禁止阻塞主线程**：音频采集、VAD、STT、LLM 调用、工具执行全部走队列/线程，UI 与状态推送永不阻塞。
3. **禁止绕过安全网关**：任何工具调用必须经 `core.execute_safely()`。禁止在工具内部直接 `subprocess.run`
   执行未校验的用户字符串；禁止 `shell=True`。
4. **强类型 + 兜底**：所有公开函数必须有类型注解与 Docstring；所有网络/子进程调用必须带超时与 `try/except`。
5. **单函数 ≤ 40 行**：超了就说明你把"机制"和"策略"揉在一起了，拆开。
6. **不引依赖**：核心链路（core/tools/ui）只允许标准库。新依赖必须写进 `pyproject.toml` 的可选 extras，
   并保证缺依赖时**优雅降级**而不是崩溃。
7. **改行为必须同时改测试与文档**：`tests/` 全绿 + 相关 `.md` 更新，才算完成。

## 4. 禁止事项（违反即回滚）

- 在仓库里写死 API Key、声纹文件、日志正文等隐私数据。
- 为"将来可能有的医生用户"在主干加 `if role == "DOCTOR"` 分支 —— 角色差异必须是**数据驱动**
  （`config/redlines.toml`），内核代码零分支。
- 引入 PyQt/Electron 之类重型 GUI 只为一个状态灯。
- 自研 Session 管理 JSON：会话状态归 tmux / `claude -c` 所有，我们只做寻址。
- 把 `dispatch()` 改成"先问 LLM 再决定要不要走快通道" —— 快通道必须永远先跑。

## 5. 验收命令

```bash
make setup     # 首次：建 .venv 并装齐依赖（之后所有 make 自动用 .venv/bin/python）
make test      # 单元 + 集成测试
make verify    # 工程验收指标（延迟/FRR/FAR/红线矩阵/行数），产出 docs/acceptance.json
make demo      # 无麦克风、无网络的端到端演示（脚本化音频）
```

后三条全绿才算可提交。缺依赖时 `make` 会直接说"先跑 make setup"，而不是抛 `ModuleNotFoundError`。
验收基线见 `TODO_ROADMAP.md` 与 `docs/ACCEPTANCE.md`。
