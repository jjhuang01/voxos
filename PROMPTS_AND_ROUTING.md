# PROMPTS_AND_ROUTING.md — 快慢双通道路由、热词增强与提示词规范

## 1. 路由总原则：确定性优先

一句话进系统后，处理顺序**永远**是：

```
文本 ──► [1] 快通道正则（<5ms，命中即执行，不问 LLM）
      └─► [2] 慢通道 LLM Function Calling（命中不了才走）
            └─► [3] 离线启发式规划器（无 API Key / 断网兜底）
```

三条通道**互相独立**，任何一条失效系统仍可用。这就是"责任链 + 兜底"。

为什么快通道必须永远先跑：用户说"我要写日志"时，意图是**确定**的。
让 LLM 去"推理"这句话，等于花 800ms 和一份 API 费去重新发现一个正则能确定的事实。

## 2. 快通道正则表（与 `tools.py` 的 `@tool(fast=...)` 一一对应）

唤醒前缀默认 `siri|jarvis|小助手|嘿 siri`，可在 `config/settings.toml` 的 `wake_words` 修改。

| 场景 | 用户说法（示例） | 正则要点 | 目标工具 |
| :--- | :--- | :--- | :--- |
| 写日志 | "Siri，我要写日志，今天修了内存泄漏" | `(我要\|帮我)?(写\|记)(一条\|个)?日志[，,：:\s]*(.+)$` | `append_structured_log` |
| 写代码 | "Siri，我要写代码，给登录接口加验证码" | `(我要\|帮我)?(写代码\|改代码\|继续写)[，,：:\s]*(.+)$` | `dispatch_code_session` |
| 查运维 | "Siri，检查一下服务器的流量和错误日志" | `(检查\|看看\|巡检)(一下)?(服务器\|系统\|线上)?(.+)$` | `inspect_server_ops` |
| 撤销 | "Siri，撤销刚才的修改" | `(撤销\|回滚\|还原)(刚才\|上次)?(的)?(修改\|改动)?$` | `rollback_file` |

**正则设计三条纪律**：

1. 必须 `^` 锚定开头（避免句中误命中），`re.I`。
2. 捕获组 1 永远是"要传给工具的第一个参数"（`@tool` 约定），多余信息交给慢通道。
3. 宁可漏，不可错：命中歧义时**放弃**快通道，让 LLM 判断。

## 3. 慢通道 System Prompt（`config/prompts/dispatch.md`）

提示词**不在代码里**。调一句「回复 ≤ 25 字」不该走一遍构建和发版，所以它是一份文件，
三层来源、后者覆盖前者：

| 层 | 路径 | 特点 |
| :--- | :--- | :--- |
| 1 | `prompts.DEFAULT_DISPATCH`（代码） | 唯一兜底，保证任何机器都起得来 |
| 2 | `config/prompts/dispatch.md`（随包） | 跟着 wheel 走，`pipx upgrade` 会换 |
| 3 | `~/.voxos/prompts/dispatch.md`（用户） | 升级不丢，**存盘即生效**（按 mtime 失效） |

角色纪律是**叠加**，不是替换：`dispatch.md` 之后接 `developer.md` / `ops.md` / `doctor.md`
（按 `workspace.role` 选）。少一段纪律顶多不听话，缺了调度规则就完全跑偏——
所以角色文件缺失只降级，不报错。

占位符只替换**认得的**那几个（`{role}` `{now}` `{active_window}` `{workspace}`
`{recent_files}` `{max_rounds}`）。用 `str.format_map` 会让用户提示词里的
`{"a": 1}` 当场 KeyError，把一句闲聊变成一次异常。

```
你是 VoxOS 的意图调度内核。运行角色：{role}
【环境】时间 {now} | 焦点窗口 {active_window} | 工作区 {workspace} | 最近文件 {recent_files}
【规则】
1. 输入来自语音转写，可能有同音错字，请结合上下文纠正技术术语（如「配置」→ config）。
2. 不是对你说的（屋里闲聊、电视声、听不清的碎片）：只输出 IGNORE，不调工具也不回复。
3. 明确在问你或让你做事：先判断能不能用工具完成；能就调用，不能就直接回答。
4. 一句话含多个意图时，同一回合发起多个 tool_calls；拿到结果后再判断要不要继续，
   最多 {max_rounds} 轮。不要为了凑轮次重复调用同一个工具。
5. 用户只说文件名时，优先在【最近文件】与【工作区】内解析，不要臆造路径。
6. 工具返回「需要确认」或「已拦截」时，把这句话原样告诉用户，不要自己重试。
7. 不解释过程、不输出 Markdown；最终回复 ≤ 25 字，会被 TTS 念出来。
```

温度固定 `0.1`。工具 schema 由类型注解自省生成，**不手写**。
`voxos prompts --role OPS` 打印每段提示词的**来源文件**和最终文本——
「我改了文件怎么没反应」第一个要看的就是它读的是不是那个路径。

### 3.1 慢通道是 agent 循环，不是一问一答

一轮 = 发一次请求 → 拿到 `tool_calls` → 执行 → 把结果回填 → 再发一次。
上限 `llm.max_tool_rounds`（默认 3）。两个必须的约束：

- **只回填三件套**（`role` / `content` / `tool_calls`）。网关塞回来的
  `provider_metadata`（路由、成本、原始 id）和 `reasoning_content`（思考链）
  一律削掉：把思考链回放给模型，它会把自己的推理当成对话的一部分继续往下编。
- **工具结果原样进上下文**。模型据此决定「还要不要再调一个」，
  所以 `execute_safely` 的返回值必须是**人能读懂的一句话**，不是 JSON。

### 3.2 渠道私有字段（Cline / sub2API）

`provider = "cline"` 时注入 `thinking: {"type": "disabled"}`。两条都是实测踩出来的：

- 模型名必须带 `cline-pass/` 前缀（`cline-pass/deepseek-v4.1-flash-1017`）。
  裸 `deepseek/*` 走付费层，余额 0 时直接 402。
- **思考必须关**。开着时 DeepSeek V4 会把思考链混进 tool_call 的参数流，
  结构化调用直接失败；关掉之后 3/3 成功。

另外网关把标准 OpenAI 响应整个包在 `{"data": …, "success": true}` 里，
`body["choices"]` 会 KeyError——而那个 KeyError 会被上层当成「模型不可用」，
于是配好了也永远走离线兜底。`core._unwrap` 负责剥这层信封。

## 4. 离线启发式规划器（断网/无 Key 兜底）

规则极简，只覆盖三大高频场景 + 撤销，命中不了就返回 `IGNORE`（宁可不做，不能乱做）：

```
含"日志|记录|记一下"        → append_structured_log(content=整句去前缀)
含"重构|大改|重写|整个项目|交给 claude" → delegate_to_coding_cli(prompt=整句)   ← red_confirm，会弹确认卡
含"文件名 + 补|加|改|删|修"  → dispatch_code_session(instruction=整句)
含"写代码|改代码|改一下|加个|修复" → dispatch_code_session(instruction=整句)
含"服务器|负载|流量|巡检"   → inspect_server_ops(query=整句)
含"撤销|回滚|还原"          → rollback_file()
否则                        → IGNORE
```

**为什么大改要单独一条**：确认通道如果没有任何语音入口，那张确认卡就是摆设
（LLM 通道默认关闭，而 `delegate_to_coding_cli` 只由它触发）。但反过来，如果小修小补也弹确认，
人就会养成闭眼点"允许"的习惯——那比不弹更危险。所以分界线画在"是不是重写整块"上。

多意图用连接词切分：`然后|接着|再|，然后`。切分后逐段独立匹配。

## 5. 热词增强（Hotword Boosting）

`stt.py` 的 `refresh_hotwords()` 每 10 分钟扫描一次工作区：

1. Git 仓库内所有文件名、目录名（去掉扩展名，按下划线/驼峰拆词）。
2. `pyproject.toml` / `package.json` / `requirements.txt` 里的依赖名。
3. 用户自定义 `config/hotwords.txt`（人工追加，优先级最高）。
4. 上限 2000 条，按"最近修改时间"排序截断，写入 `config/hotwords.txt.generated`。

注入方式：SenseVoice/FunASR 走 `hotword` 参数；OpenAI Whisper 走 `prompt` 前缀
（把热词拼成一句自然语言提示，例如"以下是可能的术语：VoxOS、声纹锁、tmux……"）。

## 6. 语义尾词等待（Premature Endpointing 修复）

VAD 判定静音后，**不立刻截断**，先看已转写文本的结尾：

```
未完结尾词 = ["我要", "然后", "把", "在", "因为", "和", "的", "如果", "帮我", "接着", "再"]
```

- 命中 → 静音阈值从 `0.8s` 动态延长到 `2.5s`，继续收音（硬上限 12s）。
- 未命中 → 正常截断。

## 7. 编程 Session 寻址（不自研状态机）

`dispatch_code_session` 的决策顺序：

1. `tmux has-session -t <name>` 成功 → `tmux send-keys` 把指令推入该会话（上下文天然续接）。
2. 不存在 → `tmux new-session -d -s <name> -c <project_dir>` 新建。
3. 会话内执行的命令：`claude -c -p "<instruction>"`（续接 Claude Code）或 `aider --message`。
4. 全程 `dry_run` 可开关：`config/settings.toml → workspace.dry_run = true` 时只打印命令不执行，
   用于无 tmux / CI / 演示环境。

**不写 `sessions.json`**。会话是否存在，问 tmux；上下文是否连续，问 `claude -c`。

## 8. 状态与播报文案

| 状态 | 胶囊文案 | TTS 播报（≤25 字） |
| :--- | :--- | :--- |
| LISTENING | `聆听中` | — |
| EXECUTING | `写日志…` / `改代码…` / `巡检…` | — |
| DONE | `完成` | "日志已记录" / "已派发到 main_coding" |
| BLOCKED | `已拦截` | "该操作已按安全红线拦截" |
| CONFIRM | `需要确认` | "这步有风险，请说确认" |

播报文案必须**陈述结果**，不陈述过程。禁止"我正在努力为您处理"这类填充词。
