# CODING_PHILOSOPHY.md — 代码品味、设计哲学与防过度设计宪章

本项目奉行 **王垠《编程的智慧》极简正交哲学**、**John Ousterhout《软件设计哲学》深模块原则**、
**UNIX KISS 原则** 与 **《设计心理学》反馈/意符/防错** 四条主脉。
本文件是最高代码审美标准，优先级高于任何"最佳实践"教条。

## 0. 一句话

> **代码的价值不在于它能做多少事，而在于下一个读它的人要花多少脑子。**

## 1. 深模块铁律（Deep Modules）

- **接口窄、实现厚**：`execute_safely(name, args)` 两个参数，内部完成红线拦截、快照备份、
  单飞串行、异常兜底。调用方不需要知道这些。
- **拒绝浅模块**：不允许 `Service → Manager → Executor → Handler` 这种只做参数搬运的链条。
  如果一个类只被调用一次、且方法体是转调，它就不该存在。
- **机制与策略分离**：机制（怎么备份、怎么发 HTTP）写在代码里；策略（哪些命令禁止、日志模板长什么样）
  写在 `config/*.toml` 里。改策略不需要改代码。

## 2. 拒绝类泛滥（No Classitis）

- 能用纯函数表达就**不许**定义 Class。`dsp.py` 里全是纯函数，没有 `DSPProcessorFactory`。
- 只有**必须持有状态或实现协议**时才用类：`SpeakerLock`（持声纹向量）、`StatusBus`（持订阅者）、
  `AudioPipeline`（持设备与计数器）、`Executor`（持锁与审计路径）。
- 判别标准不是"类有几个"，而是"这个类有没有必要存在"：
  **dataclass 只作为数据结构（无方法或仅 1-2 个纯方法）不计入"行为类"**；
  纯计算一律写成函数（`dsp.py` 里没有任何 `Processor` 类）。
- 不写 `get_xxx()` / `set_xxx()`。Python 有属性，别学 Java。

## 3. 40 行与正交性

- **单函数 ≤ 40 行**。超限信号：你在一个函数里同时处理了 I/O、业务规则和格式转换。
- **零样板扩展**：加一个能力 = 在 `tools.py` 加一个带 `@tool` 的普通函数。
  JSON Schema 由类型注解自省生成，正则快通道由装饰器参数声明。**不许手写 schema 字典**。
- **DRY 但别过度抽象**：两处相似代码好过一处错误抽象（Sandi Metz 规则）。等第三次重复再抽。

## 4. 复用操作系统原语（Leverage OS Primitives）

| 需求 | 别自己写 | 用 |
| :--- | :--- | :--- |
| 持久编程会话 | `sessions.json` 状态机 | `tmux has-session / new-session / send-keys`、`claude -c` |
| 定时/日志轮转 | 自研调度器 | `launchd` / `cron` |
| 状态持久化 | ORM | 追加式 JSONL |
| 打开文件定位行 | 自研 IDE 插件 | `code -g file:line` |

## 5. 物理世界鲁棒性四原则（写音频代码前必读）

1. **自愈重连**：任何设备采集循环必须 `while True + try/except + backoff`。
   蓝牙耳机摘下、USB Dongle 拔出、合盖休眠都会让 PortAudio 抛 `-9988`，裸跑必崩。
2. **回声门控**：TTS 播报期间及结束后 300ms 混响期内，非打断词的音频一律丢弃。
   否则 Agent 会听到自己说话 → 触发工具 → 再说话，形成自激死循环。
3. **语义尾词等待**：`"...我要写代码"` 这种半截话不能按 800ms 静音就截断。
   以未完结虚词结尾时，静音容忍窗口自动延长到 2.5s。
4. **单飞执行锁**：写文件类工具同一时刻只允许一个在跑。连续两句"改 auth.py"必须排队，不能并发写坏文件。

## 6. 可测试性是设计的一部分

- 每个深模块都必须能在**没有麦克风、没有网络、没有 LLM Key** 的机器上被测试。
  → 因此所有 I/O 都以"可注入的源/适配器"形式存在（`WavSource` / `ScriptedSource` / `ScriptedStt`）。
- 时间、随机、子进程、HTTP 都是副作用。副作用必须集中在一层，便于替换。

## 7. 反面教材（本仓库明令禁止的写法）

```python
# ❌ 为了"可扩展"而抽象
class ToolFactory:
    def create_tool(self, name): ...

# ❌ 手写 schema 与函数签名两处维护
SCHEMA = {"name": "write_log", "parameters": {"properties": {"content": {"type": "string"}}}}
def write_log(content: str): ...

# ❌ 角色分支污染内核
if self.role == "DOCTOR":
    if tool_name not in [...]: return FORBIDDEN

# ❌ 吞异常
try:
    do()
except Exception:
    pass
```

```python
# ✅ 函数即插件
@tool(fast=r"(?:写|记)日志[，,：:\s]*(.+)$", risk="green")
def append_structured_log(content: str, category: str = "DEV") -> str:
    """把口述内容按模板追加到今日 Markdown 开发日志。"""
```

## 8. 行数预算（超了要写理由，或者拆）

| 模块 | 预算 | 实际 | 职责 |
| :--- | ---: | ---: | :--- |
| `registry.py` | 160 | 144 | 工具契约、`@tool` 自省、注册表 |
| `safety.py` | 260 | 253 | 红线网关、快照回滚、执行器、审计 |
| `core.py` | 260 | 249 | 快慢双通道路由、LLM 客户端 |
| `tools.py` | 300 | 290 | 10 个原子工具（每个 ≤ 15 行） |
| `dsp.py` | 300 | 280 | 纯函数信号处理（零 I/O） |
| `audio.py` | 500 | 485 | 设备源、自愈重连、VAD/声纹/STT 适配 |
| `ui.py` | 250 | 235 | 状态总线、SSE 服务、Earcon |
| `main.py` | 300 | 296 | CLI 编排 |
| `config.py` | 120 | 100 | TOML 加载与环境变量覆盖 |
| `corpus.py` | 160 | 142 | 验收语料生成 |

> **行数不是信仰，是复杂度预算。** 数字超了，先问"我是不是在写浅模块"，再决定拆文件。

**这条规则真的触发过一次**：首版把注册表、安全网关、路由全塞进 `core.py`，长到 598 行、
28 个顶层定义。按本规则它被拆成 `registry.py`（工具契约）/ `safety.py`（安全边界）/ `core.py`（路由），
拆分后每个文件都只回答一个问题，`core.py` 通过 re-export 保持旧调用方不受影响。
不是把预算改大，而是把文件改小。
