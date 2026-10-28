# TODO_ROADMAP.md — 渐进式落地与验收清单

> 状态标记：`[x]` 已完成并验证（附证据）｜`[~]` 部分完成｜`[ ]` 未开始
> 每项都必须能在**没有麦克风、没有网络、没有 API Key** 的机器上被复现或跳过。

## Phase 1：MVP 核心闭环（本仓库 v0.1）

**阶段目标**：单机闭环，说完 500ms 内有动作，界面只有一个胶囊，安全网关不可绕过。

### Task 1.1 音频底座与声纹锁

- [x] `dsp.py`：AGC（-3dBFS 目标、24dB 上限、底噪不放大）、RBJ 高架滤波（+6dB @3kHz）、
      能量 VAD（自适应底噪）、端点判定（800ms→2500ms 语义延长、12s 硬上限）、回声门（300ms 混响期）。
- [x] `audio.py`：`ArraySource` / `WavSource` / `MicSource`（sounddevice）/ `SelfHealingSource`（2s 退避重连）。
- [x] `SileroVad`：同时兼容 v4（x/h/c）与 v5（input/state/sr）两种 ONNX 导出；模型缺失自动降级。
- [x] `SpeakerLock`：双模（normal/mask）嵌入取最大相似度；未录入 fail-closed；`npz` 持久化。
- [x] `SherpaEmbedder`（CAM++ 192 维）+ `SpectralEmbedder`（降级，含 UBM 中心化）。
- *验收*：`tests/test_dsp.py` 13 项 + `tests/test_audio.py` 14 项全绿；
  真实语料 FRR 0% / FAR 0%（阈值 0.84，7 个 TTS 音色，含同性别中文女声）。

### Task 1.2 热词 STT 与快慢双通道路由

- [x] `refresh_hotwords()`：扫描工作区文件名 → `config/hotwords.txt.generated`（上限 2000 条）。
- [x] 快通道 4 条正则：写日志 / 写代码 / 巡检 / 撤销；唤醒词统一剥离（`_wake_pattern`）。
- [x] 慢通道：OpenAI 兼容 `chat()`（零 SDK，urllib），多轮 `tool_calls` 串行执行后总结。
- [x] 离线启发式：4 条规则 + `然后/接着` 多意图切分，命中不了返回 `ignore`（宁可不做）。
- *验收*：快通道 p95 = **0.0008ms**；端到端触发 p95 = **0.53ms**；LLM 不可达时快通道不受影响。

### Task 1.3 三大生产力工具与 Session 管理

- [x] `append_structured_log`：模板化 Markdown、自动建日文件、分类标签。
- [x] `dispatch_code_session`：tmux `has-session` → `send-keys`，不存在则 `new-session`；`dry_run` 可演示。
- [x] `inspect_server_ops`：`getloadavg` + `disk_usage` + `docker ps`（3s 超时），只读，≤35 字简报。
- [x] `write_code` / `rollback_file` / `read_file_head` / `open_in_ide` / `search_notes` / `list_tools` / `delegate_to_coding_cli`。
- *验收*：`tests/test_tools.py` 11 项；`voxos demo` 真实语音跑通「日志 + 巡检」两条链路。

### Task 1.4 安全网关与极简 UI

- [x] `Guard`：数据驱动红线（11 条正则）、角色白名单（DEVELOPER/OPS/DOCTOR）、未登记工具保守拒绝。
- [x] `Executor`：红线 → 二次确认 → 快照 → 单飞锁 → 执行 → 审计，异常全部兜底为可读文案。
- [x] 快照 `.<ts>/<name>` + `meta.json` + `LATEST`；`rollback_file` 一句话回滚。
- [x] `StatusBus` + SSE `/events` + `/state` + `/command`；`web/pill.html` 四态 + reduced-motion。
- [x] 4 个 Earcon 运行时合成（正弦 + 指数包络，不携带二进制资产）。
- *验收*：红线 8/8 拦截；OPS 越权被拒；DOCTOR 强制草稿标记；`tests/test_ui.py` 6 项。

### Task 1.5 产品化补齐（审查后新增，2036-10-27）

起因：按"用户旅程能否闭环"抽查了一遍，发现入口没锁、第一次使用断、确认闸门在 GUI 里没有出口。
完整清单见 `docs/REVIEW.md`。

- [x] **本地接口鉴权**：每次运行生成随机令牌，页面渲染时注入；非本机同源 `Origin` 一律 403。
      修复前：任意网页 `fetch('http://127.0.0.1:8765/command', ...)` 可直接执行工具。
- [x] **控制面板**：胶囊点击展开为 400px 面板（状态 / 最近 / 设置），收起不留痕。
- [x] **设置层** `settings.py`：20 个字段的模式 + 范围校验 + 保留注释的 TOML 写回 + 热重载。
      写回目标跟随 `--config`（修复前总是写仓库里的默认文件）。
- [x] **首次启动闭环**：无声纹时进入 ONBOARD 态并在面板内一键录音，录完自动进入待机。
- [x] **确认闸门双通道**：终端与面板谁先回答谁生效；无 TTY 时不再秒退为"已取消"。
- [x] **热路径去阻塞**：`_context()` 从 271ms 同步 osascript 改为缓存 + 后台刷新（实测 < 0.1ms）；
      TTS 播报移出主循环。
- [x] **最近记录**：8 条（时间 / 原文 / 结果 / 路由），用于排查"它听成了什么"。
- [x] **`voxos pill`**：用 Chromium `--app` 模式开无边框小窗，最接近"悬浮胶囊"。
- [x] **打断词**：播报中喊「停 / 别说了 / 算了」真的停播报，而不是当成新指令。
      修的过程中发现两个真 bug：回声门关闭时会清空已录语音（把「停」一起丢掉）、
      端点判定在纯静音下每 12 秒造出一段假语音。
- [x] **静音待机实测**：-40dBFS 底噪跑 30 秒 → **0.32% 单核/秒音频，零误触发**。
- [x] **路由覆盖表**：`tests/test_routing.py` 22 项，16 条自然说法 + 未知句不乱动。
      补上了「把 auth.py 的空值校验补上」这类把字句，以及「Jarvis：」中文冒号唤醒词。
- *验收*：`tests/test_settings.py` 19 项 + `tests/test_ui.py` 14 项 + `tests/test_routing.py` 25 项
  + `tests/test_barge.py` 4 项；验收项从 10 项扩到 **17 项**，新增的都是抽查出来的坑。
- [x] **像素走查工具**：`scripts/shot.py`（零依赖 CDP 客户端）驱动真实页面截 8 个状态，
  同时能当探针量 `getBoundingClientRect`。本轮据此修掉 9 个界面与链路问题，
  其中两个是 P0：确认卡在说枚举值、以及**没有任何语音路径能走到确认卡**。


### Task 1.6 把「已知缺口」逐条做掉（2036-10-28）

- [x] **真实 STT**：`SenseVoiceStt`（sherpa-onnx，int8 229MB）接进 `make_stt()`，
      设置页 `stt.backend = sense_voice` 即用；缺模型退回 `scripted` 并给出人话错误。
      实测 24 句 818 字：字符错误率 **2.8%**（中文 1.4%、英文 3.1%），**≥30× 实时**。
      中文那 1.4% 全是「十点→10点」的数字规范化，语义上零错；英文按词算是 **12.5%**
      （`null`→`Ill`、`auth`→`Oth`），证明热词注入是必需的。
- [x] **多意图切分接进快通道之前**：原先快通道抢到第一件事就返回，把后半句悄悄吃掉。
      现在先数段数，多段则逐段规划；路由标签也照实标（全走正则 = `fast`，有一段落到启发式 = `offline`）。
- [x] **设置撤销**：保存前把旧文件留一份到 `.voxos/settings.bak`，`POST /api/settings/undo` 退回一步，
      退完备份消费掉、按钮消失。端到端验过：0.82 → 保存 0.90 → 撤销 → 0.82 → 再撤销 409。
- [x] **launchd 常驻**：`scripts/install_launchd.sh`（`--print` / `--status` / `--uninstall`），
      `KeepAlive{SuccessfulExit=false}`，`plutil -lint` 通过。脚本头部写清 macOS 的 TCC 陷阱：
      launchd 弹不出麦克风授权框，必须先手动跑一次 `voxos run` 授权。
- [x] **截图工具的第三课**：headless Chrome 不产帧，CSS `transition` 会永远停在起始值，
      `getComputedStyle` 读到的是改之前的样子。`shot.py` 加 `settle()` 先逼两帧再截。
- *验收*：验收项 +1，测试 +16，`core.dispatch` 一度涨到 43 行被验收项 #9 逮住，
  提成 `_multi_intent()` 后回到 39 行。
  （这一轮起，每轮只记**增量**；绝对值只写在 Task 1.8 的工程化一节，否则每加一轮，
  上一轮的「→ N 项」就变成一句谎话。）


### Task 1.7 把「已修」重新量一遍（2036-10-28 夜）

这一轮不加功能，只回头查上一轮的结论还成不成立。查出三件事。

- [x] **管道死锁（P0）**：`force_end` 后转写为空时，`_finalize()` 返回 `None`，
      调用方当成「还没说完」不清缓冲，下一块音频把整段重拼重解一次。12 秒后吞吐
      从 51 块/秒掉到 7 块/秒、CPU 涨到 95%，永不恢复。一声咳嗽就能触发。
      修法：收尾永远返回一段（空转写也返回 `Utterance(accepted=False)`）。
      回归测试把修复退回去要跑 **117 秒**才失败，病征复现得很干净。
- [x] **指标测错了对象（P1）**：原来报的「英文词级 87.5%」来自英文句子 + 英文音色，
      量的是英文识别；真正的中英混说（中文句子夹术语、中文音色）是 **29.4%**。
      对照实验证明是发音而非模型：同一批句子交给英文音色，`docker`/`build`/`review`
      全部正确。验收表新增一项并标为 **GAP**（未达标、非回归）。
- [x] **清单项不存在**：界面没有 `pointerdown`/`mousedown`/`touchstart`，
      既没有拖拽也没有长按。需要守的是 `drain` 倒计时条（必须等于
      `workspace.confirm_timeout_s`，实测 8s = 8s）和 `prefers-reduced-motion` 降级。
- [x] **界面回归探针**：`scripts/audit_ui.py`，把审查里「量出来的数」变成 12 条断言
      （`#19` `#20` `#21` `#22` `#26` `#27` `#28` `#29` `#30` `#35`×2 + 确认卡完整可见）。
      故意退回两个修复，探针精确复现原始症状（`display=flex`、差 16px）。
- [x] **headless 默认就在减弱动效里**：`matchMedia('(prefers-reduced-motion: reduce)')`
      在 headless Chrome 里默认返回 **true**。两个后果：① 之前 10 张截图全是在
      减弱动效下拍的，呼吸点/电平条从没动过；② 只量 reduce 状态的检查等于没检查。
      `shot.py` 显式关掉它再截，`audit_ui` 那条改成**两个状态都量**（差分）。
- [x] **长稳脚本**：`scripts/soak.py`，实时节奏喂音频、按分钟采样内存/CPU。
      不依赖 `ps`（沙箱里起不来），用 `proc_pid_rusage` 读 `phys_footprint`。
- [x] **长稳指标改口径**：第一版读 RSS，25 分钟里在 **28MB 和 364MB 之间乱跳**
      （28MB 比裸 Python 还低，而进程里装着 229MB 模型）。真因：模型权重是 mmap
      的文件页，`resident_size` 数的是页缓存命中率，不是占用。改成 `phys_footprint`
      （含已压缩页）后稳定在 400MB 量级。顺带：`proc_pid_rusage` 没有长度参数，
      按结构体精确开 80 字节缓冲会被内核写穿 → 段错误，给 1KB。
- [x] **25 分钟长稳结果**：内存 406.7 → 407.5 MB（跨度 **0.8MB**），CPU 均值
      **11.7%**，75001 块严格线性（无缓冲堆积），231 段全部被声纹锁接受。
- [x] **35 条缺陷逐条对到守卫上**：34 条有可重跑的东西守着（测试 / 界面探针 /
      验收指标），剩下 1 条（截图工具）守的本来就不是产品代码路径。
      会静默退化的 3 条守卫都做过注入回归，确认会红。
- [x] **读日志不是写日志**：`heuristic_plan` 只要出现「日志」两个字就判成写日志，
      「看一下日志」「日志在哪」都会往日记里写一条。改成要动词（`写|记…日志|记录`）。
      顺带：运维快通道的服务器名词是**可选**的，于是「看看」+ 任何东西都命中，
      「看看今天的日志」去查了服务器负载——名词改必需，补上负载/流量/监控/内存。
- [x] **中文的「删掉」在红线里是空白**：红线 11 条全是 `rm -rf` 这类命令式写法，
      「删掉 auth.py」会走 YELLOW 的写代码通道，不弹确认就送进 coding CLI。
      新增 `[confirm]` 词表：YELLOW 工具参数里出现不可逆动作 → 升级为二次确认。
      只作用于 YELLOW——GREEN 工具改不了东西，不该因为措辞被拦。
- [x] **混说指标给区间**：补一版双语拼接语料（中文说中文、术语说英文），
      29.4% → 29.4% / **47.1%**。结论不变（都远低于 95%），但只报低的那版是把模型
      说得比实际差。两版都进 `make verify`。
- [x] **文档数字守卫**：`tests/test_docs.py` 5 条。这一轮测试数改了三次、漏了三次
      （README 161 / 路线图 160 / 验收表 157），根因是历史数字和当前数字写法相同。
      改成每轮只记增量，绝对值只写一处。守卫写完立刻抓到我自己刚改的 `corpus.py`。
- *验收*：验收项 +1（新增 GAP 档），测试 +16，界面探针 +4，长稳脚本 +1，文档守卫 +5。


### Task 1.8 工程化

- [x] 216 项测试（`make test`），`scripts/verify.py` 19 项验收指标（`make verify`），
      `scripts/audit_ui.py` 12 项界面回归（`make audit-ui`），`scripts/soak.py` 长稳（`make soak`）。
- [x] 全模块单函数 ≤ 40 行（最长 39 行）、核心五模块共 8 个类、核心零第三方依赖。
- [x] 文档：AGENTS / CODING_PHILOSOPHY / PRD / REDLINES / PROMPTS_AND_ROUTING / DESIGN_SYSTEM / README / BUSINESS。

---

## Phase 2：跨网全双工与行业插件（v0.2）

- [ ] **Task 2.1 全双工隧道**：FastAPI + WebRTC（LiveKit）低延迟双向音频；手机 PWA（一个大呼吸球按钮）。
      验收：4G 下首字延迟 < 800ms，断网 5s 内自动重连。
- [ ] **Task 2.2 面罩声学校准向导**：录 10 句戴面罩语音 → 拟合 EQ 曲线 → 写入 `config/mask_profile.json`。
      验收：戴面罩识别率从 84% 提升到 ≥ 97%（自建测试集）。
- [~] **Task 2.3 真实 STT 接入**：本地 SenseVoice-Small（ONNX）已接（见 Task 1.6），
      还差 faster-whisper 第二后端、热词注入、流式首字 < 300ms。
      验收：中英混说 + 代码术语识别率 ≥ 95%（**实测 29.4%**，见下方缺口表）；流式首字 < 300ms。
      备注：`refresh_hotwords()` 只喂 `openai` / `local_whisper` 两个后端，
      **SenseVoice 在 sherpa-onnx 里没有热词偏置接口**，所以本地后端拿不到热词。
- [ ] **Task 2.4 医疗/OPS 插件包**：科室词库、只读 EHR 草稿箱、审计导出。
      验收：DOCTOR 角色无法触达任何系统修改工具（已有测试，需补真实插件）。

## Phase 3：软硬一体与商业化（v1.0）

- [ ] **Task 3.1 云端中继**：一键扫码配对，免 Tailscale 配置；按分钟计费。
- [ ] **Task 3.2 VoxMask 硬件**：磁吸静音面罩 + 硬件级声纹芯片 + 物理"紧急静音"键。
- [ ] **Task 3.3 个人记忆图谱**：日志/会话向量化，支持"上次那个 bug 我是怎么修的"。

---

## 已知缺口（诚实清单）

| 缺口 | 影响 | 下一步 |
| :--- | :--- | :--- |
| 中英混说术语召回 **29.4% / 47.1%**（目标 95%） | 中文句子夹 `docker`/`nginx` 会被听成 `care`/`in`；中文本身没问题（字错 9.6%）。两个数是两种语料：中文音色念术语 / 双语拼接，真人落在两者之间 | 支持热词偏置的双语模型，或真人发音样本重新标定（见 REVIEW §27） |
| 真人声纹未标定 | 现有 FRR/FAR 来自 6 个 TTS 音色，真人未知 | 录 20 条真人音频重跑 `make verify` |
| 长稳只跑了 25 分钟、且不含设备层 | 管道级 25 分钟内存跨度 **0.8MB**、CPU 11.7%、块数严格线性（无缓冲堆积）；麦克风热插拔、蓝牙切换、休眠唤醒没覆盖 | 真挂机 8 小时 + 插拔设备 |
| 降级声纹（无 CAM++ 时）精度不足 | 仅能区分明显不同音色 | 强制引导下载模型，或降级为"仅日志模式" |
| 无手机端 | 只能在电脑旁说话 | Phase 2 |
| 多意图的语义切分仅靠连接词 | "记日志，顺便看下负载" 可切分，"帮我搞定" 不行 | 慢通道兜底 + 语料扩充 |

> 已关闭：「面板未做像素级走查」（`scripts/shot.py` + 10 张真实截图 + `audit_ui.py` 12 条断言）、
> 「没有打断词」（回声门 + 打断词白名单 + `stop_speaking()`，见 `tests/test_barge.py`）、
> 「未接真实 STT」（SenseVoice int8，2.8% / ≥30× 实时，见 `docs/REVIEW.md §15`）、
> 「macOS 麦克风权限 / launchd 未自动化」（`scripts/install_launchd.sh`）、
> 「设置无历史 / 撤销」（`settings.bak` + `POST /api/settings/undo`，见 `docs/REVIEW.md §19`）、
> 「拖拽 / 长按 / 动效逐帧」（界面里没有拖拽和长按，清单项本身写错了，见 §23）、
> 「管道 12 秒后卡死」（§21，一声咳嗽就能触发）、
> 「读日志被当成写日志 / 中文『删掉』不弹确认 / 文档数字对不上」（§27）。
