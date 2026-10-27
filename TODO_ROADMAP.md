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

### Task 1.5 产品化补齐（审查后新增，2026-10-27）

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


### Task 1.5 工程化

- [x] 141 项测试（`make test`），`scripts/verify.py` 17 项验收指标（`make verify`）。
- [x] 全模块单函数 ≤ 40 行、类 ≤ 12 个、核心零第三方依赖。
- [x] 文档：AGENTS / CODING_PHILOSOPHY / PRD / REDLINES / PROMPTS_AND_ROUTING / DESIGN_SYSTEM / README / BUSINESS。

---

## Phase 2：跨网全双工与行业插件（v0.2）

- [ ] **Task 2.1 全双工隧道**：FastAPI + WebRTC（LiveKit）低延迟双向音频；手机 PWA（一个大呼吸球按钮）。
      验收：4G 下首字延迟 < 800ms，断网 5s 内自动重连。
- [ ] **Task 2.2 面罩声学校准向导**：录 10 句戴面罩语音 → 拟合 EQ 曲线 → 写入 `config/mask_profile.json`。
      验收：戴面罩识别率从 84% 提升到 ≥ 97%（自建测试集）。
- [ ] **Task 2.3 真实 STT 接入**：SenseVoice-Small（本地 ONNX）与 faster-whisper 双后端 + 热词注入。
      验收：中英混说 + 代码术语识别率 ≥ 95%；流式首字 < 300ms。
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
| 未接真实 STT（默认 `scripted`） | 语音识别靠外部后端，本地无模型时只能跑演示脚本 | Task 2.3 |
| 未在真麦克风上做 8 小时长稳测试 | 只有 30 秒底噪的 CPU 采样，没有小时级内存曲线 | 用 `voxos run` + `psutil` 采样补测 |
| 真人声纹未标定 | 现有 FRR/FAR 来自 6 个 TTS 音色，真人未知 | 录 20 条真人音频重跑 `make verify` |
| 降级声纹（无 CAM++ 时）精度不足 | 仅能区分明显不同音色 | 强制引导下载模型，或降级为"仅日志模式" |
| 无手机端 | 只能在电脑旁说话 | Phase 2 |
| macOS 麦克风权限/launchd 未自动化 | 首次运行需手动授权 | 补 `scripts/install_launchd.sh` |
| 多意图的语义切分仅靠连接词 | "记日志，顺便看下负载" 可切分，"帮我搞定" 不行 | 慢通道兜底 + 语料扩充 |
| 设置无历史/撤销 | 改错了只能再改回去 | 保存前把旧文件复制到 `.voxos/settings.bak` |
| 面板只覆盖了静态与 hover/焦点 | 拖拽、长按、动效逐帧没看（`scripts/shot.py` 已能派发鼠标事件） | 补 `Input.dispatchMouseEvent` 的拖拽序列 |

> 已关闭：「面板未做像素级走查」（`scripts/shot.py` + 8 张真实截图，见 `docs/REVIEW.md §12-14`）、
> 「没有打断词」（回声门 + 打断词白名单 + `stop_speaking()`，见 `tests/test_barge.py`）。
