# 变更记录

本文件是 Release 说明的唯一来源：`scripts/release_notes.py <版本>` 从这里抽出对应段落，
CI 的 release job 用它当 `gh release create --notes-file`。所以**发版前必须在这里写一段**，
否则 tag 打上去，Release 页面会是空的。

格式约定：`## [x.y.z] - YYYY-MM-DD`，一个版本一段，从新到旧。

## [0.1.23] - 2026-10-29

这一版全在「说出来」：沙箱结论、启动横幅、失败原因，以及 App 的退出路径。

### 修复

- **启动横幅押在模型加载之后**。横幅里有「命令沙箱=… 生效/不生效」，但它原来
  排在 `make_embedder`（CAM++）+ 热词刷新之后——这两步可能阻塞几十秒（第一次
  还会弹麦克风授权等人点）。日志看起来就是「服务在跑，但什么都没说」，而
  「沙箱到底生不生效」正是最不该等的那一条。现在拆成两行：胶囊地址 + 沙箱结论
  在服务就绪后**立刻**打，模型信息加载完再打。两行都 `flush=True`。

- **App 日志被块缓冲吞掉**。启动器把 stdout 重定向到文件，Python 就改成块缓冲，
  横幅和 traceback 都卡在缓冲区里，进程活多久憋多久。启动器加 `PYTHONUNBUFFERED=1`。

- **守护进程死了，窗口和启动器留在原地**。启动器原来 `wait "$WINDOW_PID"`，
  只等窗口。守护进程先死的时候它一动不动，窗口挂着一个死掉的页面。更糟的是
  App 的可执行文件就是这个启动器，LaunchServices 认为它还在运行，**再双击只
  会「激活」它，不会起新的**——用户看到「点了没反应」，而且没法自救。
  现在改成轮询等**任意一个**先死再带走另一个（macOS 自带 bash 3.2，没有
  `wait -n`）。副作用是 `voxos stop` 现在会把整个 App 一起收掉。

- **启动失败弹窗只说「日志在 ~/.voxos/logs/app.log」**。用户此刻还不知道「日志」
  是什么，等于让他自己去找。现在弹窗里直接带上最后一行错误。顺带删掉一句
  `tail -5 "$LOG" >/dev/null`——读了日志又扔掉，纯死代码。

## [0.1.22] - 2026-10-29

### 修复

- **沙箱「装好了但悄悄没生效」这件事现在会自己说出来**。0.1.21 只是把失败判定
  收紧了，用户仍然不知道自己的命令到底有没有被关进笼子。现在进程启动后探一次
  `sandbox-exec` 能不能真的套上 profile、缓存结论，`voxos doctor` 与启动横幅都报
  「生效 / 不生效 + 为什么」，`run_command` 按结论走而不是先试一次。

- **可写根画错了**。`workspace-write` 原来只放行 `~/Projects` + `~/Documents/DevLogs`
  + 临时目录——用户在自己项目里说「跑一下测试」，pytest 连 `.pytest_cache` 都写不
  进去，报错和「命令有 bug」长得一模一样。现在按 codex 的语义补上**启动目录**，
  并新增 `workspace.write_roots` 配置项。

  排除条件也踩了一次坑：一开始写成「cwd 必须在家目录下才算」，于是项目放在外置盘
  （`/Volumes/Work/…`）或 `/opt` 的人照样被拒。最终规则是**只排 `/`、家目录本身、
  以及家目录的祖先**——`cd /Users` 之后一条 `(subpath "/Users")` 就等于放行整个
  家目录，那个必须排；外置盘本来受 Unix 权限和 SIP 管，放开不会凭空多出写权限。

- **`/dev` 从整段放行改成逐个节点**。`(subpath "/dev")` 等于允许
  `dd of=/dev/disk2`（要 root 才生效，但没必要开）。现在只开
  `/dev/null`、`/dev/zero`、`/dev/random`、`/dev/urandom`、`/dev/stdout`、
  `/dev/stderr`、`/dev/tty` 这些 `literal`，外加 `/dev/fd`（进程替换的地基）。

- `voxos doctor` 新增「可写根」一行：边界不写出来，用户只能靠「为什么写不进去」猜。

### 调研结论（这次「沙箱起不来」的根因）

**macOS 不允许嵌套收紧。** 进程已经在某个沙箱里时，`sandbox_apply` 对任何比现状
更严的 profile 都返回 `EPERM`。本机实测矩阵（macOS 27.2 / build 26B5086k / arm64）：

| profile | 结果 |
| :--- | :--- |
| `(allow default)` | ✅ `ok` |
| `(allow default)(deny file-write*)` | ❌ `Operation not permitted` |
| `(allow default)(deny network*)` | ❌ |
| `(deny default)(allow file-read*)` | ❌ |
| `(allow default)(deny sysctl-write)` | ❌ |

所以这不是 profile 写错了，是**执行环境本身在沙箱里**。用最小 `.app` 经
LaunchServices 启动（`open -a`）拿到干净上下文后，`sandbox.status()` 返回
`(True, '')`，七项 A/B 全部符合预期（cwd 在项目里可写、cwd 在 `/` 时同一路径被拒、
`/dev/null` 可写、`~/Desktop` 被拒、`$TMPDIR` 可写、`curl` 断网）。

顺带排除了三条死路：`launchctl submit`（无输出）、`launchctl bootstrap gui/501`
（`Bootstrap failed: 5`）、`open -a Terminal x.command`（命令不执行）——沙箱上下文
会继承，只有真正被 LaunchServices 拉起的 app bundle 是干净的。

## [0.1.21] - 2026-10-29

### 修复

- **沙箱失败判定收紧**。原来只要 stderr 里出现 `Operation not permitted` 就当成
  「沙箱没起来」，于是降级重跑一遍——而**命令自己**因为权限失败时也会这么说。
  那条命令被跑两次，副作用翻倍。现在只认 `sandbox_apply` 与 `sandbox-exec:` 前缀
  （即 sandbox-exec 本人在抱怨），并加了 4 条断言把这个区分钉住。

## [0.1.20] - 2026-10-29

这一版解决三件事：**打开慢**、**只能听懂不会动手**、**「听不懂」其实没告诉用户缺什么**。
用户的原话：「我对性能有极致要求……我希望有终端命令能力，打开文件能力，写入内容能力，
有类似 Computer use 能力，比如我让打开微信并发送信息给某人，定时给某人发信息……
不要自己虚构，猜测，而是直接抄作业，能抄先抄」。

### 新增

- **胶囊窗口换成原生 Swift + WKWebView**（`native/VoxOSWindow.swift`，79 行）。
  双击到窗口出现 **2.91s → 1.21s**，少 300MB 内存。`swiftc -O -whole-module-optimization`
  编出 83KB 二进制，`NSApplication.setActivationPolicy(.accessory)` 不占 Dock，
  `canJoinAllSpaces` 跟着你换桌面，关窗即退、不留看不见的常驻。
  **踩到的坑**：WKWebView 默认拒绝一切 `http://`，没有
  `NSAppTransportSecurity → NSAllowsLocalNetworking` 时窗口起得来、进程在、**内容永远白屏**——
  所以那份 plist 用 `-sectcreate __TEXT __info_plist` 编进二进制，不是装饰。
- **命令沙箱（`src/voxos/sandbox.py`）**，三档对齐 codex 的 `sandbox_mode`：
  `read-only` / `workspace-write`（默认，只能写工作区与临时目录，**默认断网**）/
  `danger-full-access`。执行器是 macOS 自带的 seatbelt，**不是**「命令里有没有 `rm -rf`」——
  shell 是一棵树，`$IFS`、变量拼接、`sh -c`、`python -c` 绕过字符串匹配的方式无穷。
  沙箱应用不起来时**照跑但说出来**（返回文本第一句就是「沙箱没能应用」），
  不静默降级：用户以为有沙箱、实际没有，比一开始就说清楚危险得多。
- **`run_command`**：真的执行 shell。配置拼错（`workspace_write` / `disabled`）一律按
  `read-only` 处理——拼错不能等于「全开」。
- **`open_path`**：用系统默认程序打开文件/文件夹，或在访达里定位。这是「打开文件」
  和「在 IDE 里打开」之间的那个缺口。
- **`send_message`（微信 / 企业微信 / 信息）**。微信 Mac 版**没有 AppleScript 字典**
  （`sdef` 返回空），拿不到 `send message to buddy`，所以只能 System Events 模拟按键：
  激活 → ⌘⇧F 搜索 → 输入联系人 → 回车进会话 → 输入正文 → 回车。
  这条路是抄社区 wechat-auto-sender 那类脚本的，代价也照实说：
  缺权限时返回「系统设置 → 隐私与安全性 → 辅助功能」，而不是一句「已发送」。
  （`-10004 权限违例` 是中文系统上的原话，一并认。）
- **定时任务（`src/voxos/scheduler.py` + `schedule_task` / `list_schedules` /
  `cancel_schedule` / `voxos schedule`）**。不用 cron / launchd（要写系统配置、要权限、
  改完要重载），也不用 `threading.Timer`（进程一重启就没了）。落一个 JSON + 5 秒轮询，
  删掉文件等于清空。存的是**那句话本身**而不是「工具名 + 参数」：到点重新走一遍派发，
  路由、提示词、新加的工具全都自动生效。确认只认「创建任务时点过头」——
  否则一次「每天早上发消息」会在半夜弹一张没人看的确认卡，超时按拒绝，
  用户永远不知道它没发出去。

### 修复

- **`workspace.dry_run` 默认改成 `false`**。出厂默认 `true` 意味着第一次双击后
  说「打开微信」只得到一句「（dry-run）会打开微信」——演示模式是给开发者的，
  不该是给用户的默认值。
- **「听不懂」不再把系统问题说成用户问题**。正则快通道只认十来种说法，复杂句子全靠
  慢通道；没配大模型时，用户说一句复杂的话只会听到「还没学会这句」，然后换个说法再失败一次。
  现在这句话是：能力清单 **+** 「复杂句子要先接大模型（现在缺 API Key）：voxos llm --set-key」。
- **`rm -rf <目录>` 现在会先问一句**。原来的不可逆词表只认中文（删掉/清空/格式化），
  有了 shell 之后 `rm -rf build` 是一条**不经过任何确认**的删除。词表补上
  `rm -r/-rf/-f` 与 `git clean -f`。
- **`parse_at` 的两个坑**：`09:30` 原来会算出「今年 1 月 1 日 09:30」（拿 `%H:%M`
  解析出来的月日是 1 月 1 日）然后永远判成过去；`10-29 09:00` 在 Python 3.13 上会触发
  「缺年份」弃用警告，3.15 起直接报错。前者改成补今天日期并顺延到明天，后者补当年再解析。

### 性能

- **`import numpy` 从命令行路径上摘掉**。`main.py` 原来在顶层导入 `audio` / `corpus` / `dsp`，
  于是 `voxos --version`、`voxos text`、`voxos schedule` 这些根本不碰音频的命令，
  每条都要先付 58ms 的 numpy。改成谁用谁在里面导：
  `import voxos.main` 106.6ms → **83.9ms**，`voxos --version` 160–190ms → **120ms**。
  老实说：这一项的全部收益是 40ms，不是 10 倍。

### 文档

- `docs/PERFORMANCE.md` 补 §6「依赖层面」与窗口实测；`docs/PRODUCT.md` 新增，
  把这一轮的产品决定连同**代价**逐条写下来（含还没做的诚实清单）。
- 新增测试：命令沙箱档位与失败降级、`run_command`、`open_path`、AppleScript 转义、
  权限错误映射、定时任务的解析/持久化/顺延/取消、调度线程真的会跑、
  以及「每个注册的工具都必须在 `redlines.toml` 登记风险等级」（双向，删了工具忘了删登记也会红）。

## [0.1.19] - 2026-10-29

这一版接上了大模型，并把「提示词」从代码里搬了出来。用户的原话：
「另外可以接入大模型，大模型默认用cline渠道，1017；关闭思考模式，具体看sub2API；
而且这可能要有agent来处理？内置提示词系统」。

### 新增

- **大模型慢通道开箱可用：默认 Cline 渠道 `cline-pass/deepseek-v4.1-flash-1017`**。
  三条实测结论写进了配置注释里，每一条都是踩出来的：
  - 模型名**必须带 `cline-pass/` 前缀**。裸 `deepseek/*` 走付费层，余额 0 时直接 402。
  - **思考必须关**（`thinking: {"type": "disabled"}`）。开着时 DeepSeek V4 会把思考链
    混进 tool_call 的参数流，结构化调用直接失败；关掉之后 3/3 成功。
  - 网关把标准 OpenAI 响应整个包在 `{"data": …, "success": true}` 里。
    `body["choices"]` 会 KeyError，而这个 KeyError 被上层当成「模型不可用」——
    于是配好了也永远走离线兜底，报错还完全指错方向。现在 `core._unwrap` 负责剥信封。
- **`voxos llm` / `voxos llm --set-key` / `voxos llm --check`**。`--check` 是**真发**一次：
  余额 402、模型名写错、网关套信封，只有发出去才知道。密钥落到 `~/.voxos/llm_key`（0600），
  不进仓库——源码布局下 `settings.toml` 就是仓库里那份，填一次密钥等于往公开仓库推一次。
- **内置提示词系统（`src/voxos/prompts.py` + `config/prompts/*.md`）**。
  调一句「回复 ≤ 25 字」不该走一遍构建和发版。三层来源、后者覆盖前者：
  代码里的 `DEFAULT_DISPATCH` → 随包 `config/prompts/dispatch.md` →
  用户 `~/.voxos/prompts/dispatch.md`（升级不丢，存盘即生效，按 mtime 失效）。
  角色纪律（DEVELOPER / OPS / DOCTOR）是**叠加**而不是替换：少一段纪律顶多不听话，
  缺了调度规则就完全跑偏。`voxos prompts --role OPS` 打印每段的来源文件和最终文本。
- **状态页不再替模型撒谎**。原来只有「大模型：已启用 / 离线」两档，而「开了但缺 Key」
  和「真能回话」在界面上长得一模一样。现在报具体原因（缺 API Key / 缺接口地址 / 未启用）
  和模型短名。

### 修复

- **思考链不再回放**。`messages.append(message)` 原样把网关返回的整个 message 塞回对话，
  里面带着 `reasoning_content` 和几十行 `provider_metadata`。把思考链回放给模型，
  它会把自己的推理当成对话的一部分继续往下编——现在只回填 `role` / `content` / `tool_calls`。
- **没配 Key 时不再白等一次超时**。`_try_llm` 现在先问 `llm_ready`：缺钥匙就一个请求都不发，
  直接走离线兜底（本地 Ollama 不需要 Key，不受影响）。
- **一条路由测试原来会发真实网络请求**。`test_unknown_speech_still_explains_what_it_can_do`
  用的是 `load()` 读本机真实配置——大模型一配好，这条本该 1 毫秒的断言就变成一次真实请求，
  红的原因还和被测行为毫无关系。改成显式关掉慢通道。
- **启动器少起一次解释器**。App 启动时为了读一个端口号要跑 `voxos port`，
  那条命令会 import numpy / sherpa / sounddevice。改成用 `sed` 只读 `[ui]` 段的 `port`，
  就绪轮询也从 250ms 收到 100ms。

### 文档

- 新增 **`docs/PERFORMANCE.md`**：启动分段、常驻内存、VAD 与识别的真实成本，
  以及「要不要换 Rust」的结论（不整体重写，理由用数字说话）。
- 新增 **`scripts/bench.py`**：`boot` / `mem` / `audio` 三个子命令，
  把性能文档里的每个数字都变成一条可复现的命令。
- `PROMPTS_AND_ROUTING.md` 第 3 节重写：提示词三层来源、agent 循环的两条硬约束、
  Cline 渠道私有字段的来龙去脉。

## [0.1.18] - 2026-10-28

### 修复

- **胶囊窗口开不出来，而且不给任何提示**。`_open_app_window()` 只要 `open -na` 没抛异常
  就返回成功——可是 `open` 会返回 0 却什么都不弹：配置目录被上次强杀留下的
  `SingletonLock` 指向一个已死的 pid、Chrome 正在升级、企业策略禁用多实例，都会这样。
  于是「点了图标什么都没发生」，而回落到默认浏览器的那条路永远走不到。
  - 启动后**等窗口真的出现**（有进程正在用这个配置目录）才算成功，等不到就回落。
  - 启动前清掉**指向死进程**的单例锁；只在确认没人在用这个配置目录时才清——
    反过来的话会同时起两个实例，两个都以为自己独占，配置目录就真的坏了。
  - 两条都有测试：有人在用时一个文件都不许动；等不到窗口必须返回失败。

## [0.1.17] - 2026-10-28

### 改变交互形态：从「打字框」变成「一直在听」

用户的原话：「我想要的是，像天猫精灵这种，一直监听，当然我可以关闭，然后识别我说的话
转成文字，然后转成指令；总之现在的交互很奇怪；要输入文字而且听不懂」。

拆成三件事，逐条修。

### 新增

- **持续监听的总开关**。胶囊右侧多了一个静音键，状态页最上面多了一条开关，
  两处都打 `POST /api/listen`。关掉之后**麦克风仍然继续读**——只是音频不再进入
  VAD/识别队列：停止读设备会让 CoreAudio 的缓冲堆起来，恢复时一次性吐出几十秒旧音频。
- **胶囊下面常驻两行**：上一行是它听到的原话，下一行是它的回应。常驻监听时这才是
  主要反馈——不给的话用户只能打开面板翻记录才知道有没有听对。结果停 4 秒后自动
  回落到「聆听中」，不留一个永久挂着的「完成」。
- **两个日常指令**：`open_app`（打开微信 / 终端 / Safari / 打开 github.com）、
  `tell_time`（几点了 / 今天几号 / 星期几）。找 `.app` 按「完全相同 > 前缀 > 包含」
  排序，同级取名字最短的——纯子串匹配会让「微信」命中 `wechatwebdevtools`
  （微信开发者工具），因为它在字母序里靠前。
- **闲聊出口**：问候、道谢、问身份各有自己的回话（`你好啊` → `在。`），
  不再一律回「没听懂」。

### 修复

- **听不懂和做成了长得一模一样**。以前无论结果如何，胶囊上都写「完成」，
  用户看到的就是「没反应」。现在按路由分：`ignore` → 灰色 `没听懂`（中性色，
  不是错误色：屋里随便一句话都可能落到这里，红色会把正常闲聊渲染成故障），
  `chat` → `回话`，其余 → 绿色 `完成`。
- **听不懂时会朗读**。常驻监听把屋里所有闲聊都收进来，每句都念一遍
  「还没学会这句」很快就变成噪音。现在 `ignore` 只显示、不朗读。
- **「没听懂」的文案有两份**（`main.NO_ACTION` 与 `core._NO_MATCH`），
  必然有一天对不上。合并为 `core.NO_ACTION` 一处，`main.NO_ACTION` 只是别名。

## [0.1.16] - 2026-10-28

### 修复

- **装完之后 VAD 静默降级**。`onnxruntime` 只写在 `models` extra 里，而 `install.sh`
  只注入 `sherpa-onnx sounddevice`——于是 Silero VAD 加载失败，回落到能量 VAD。
  能跑，但一声咳嗽、一次关门都会被当成说话，而日志里只有一行
  `[voxos] Silero VAD 加载失败，降级能量 VAD`，很容易被当成正常输出忽略。
  - 安装脚本补上 `onnxruntime`（它不是一个可选优化：模型文件下了却用不上）。
  - `doctor` 增加「VAD 后端」一行，降级当场可见。
- 这条是在真机把整条链路跑通之后才发现的：`voxos run` 已经能听懂
  「Siri，检查一下服务器负载」并回复「负载 7.4/5.7，磁盘 94%…17ms」，
  而同一份日志里就躺着那行降级警告。

## [0.1.15] - 2026-10-28

### 新增

- `VoxOS.app`：macOS 应用包，双击即用。`scripts/build_app.sh` 生成图标、写 Info.plist
  （含麦克风用途说明）、ad-hoc 签名（TCC 靠签名把麦克风授权记在这个 App 上），
  `--install` 直接装进 `/Applications`。启动器没有终端，失败会弹窗而不是静默退出。
- `audio.input_device`：指定输入设备（索引或名字子串）。本机的系统默认输入是一个
  没插东西的音频接口，录得到帧、峰值恒为 0.0000——一切"正常"，就是听不见人说话。
- `voxos stop`：停掉在跑的守护进程（App 退出时也会带走）。
- `doctor` 多一行"输入设备"，直接回答"现在这个配置能不能听到人声"。

### 修复

- **录完声纹就被自己拒之门外**。真麦克风实测：只录 1 句时，同一个人的另一句话
  相似度只有 **0.751**，而默认阈值 **0.82**——用户按引导录完 3 秒，之后每句话都会被
  当成别人丢掉，而且没有任何提示说明为什么。
  - 录入改成**多句**：录 10 秒，VAD 按句切段（实测 2~3 段），取嵌入均值。
    一次录音切成 2~3 段**没有用**（同人 0.70~0.72、异人 0.66~0.72，间隔 0.05 以内），
    必须是多句。
  - 阈值 0.82 → **0.75**：真麦克风实测同人最低 0.788 / 异人最高 0.682，取中点。
    修完间隔从 0.007 拉到 **0.106**。
  - 录入结果回报切了几句，只切出 1 句时明确提示"模板不稳，再说 2 句"。

### 工程

- `dry_run` 默认仍是 `true`（安全），但本机使用需要手动关掉，否则工具只打印命令不执行。
- 测试 232 项（231 passed + 1 skipped）。

## [0.1.14] - 2026-10-28

### 新增

- `scripts/install.sh`：一条命令装好或升级（取包 → 安装 → 音频依赖 → 自检）。
  支持三种用法：不带参数装 GitHub 最新 Release、传本地 wheel、`--no-audio` 只要核心。
- `CHANGELOG.md`：这份文件。此前 6 个版本的变更只散在 commit message 里，
  装完想知道"这版修了什么"没地方看。

### 修复

- **升级会断在第二步**：`pipx install --force` 在 pipx 1.15 + uv 后端下重建 venv 时
  报 `error: A virtual environment already exists at: .`，然后 pipx 拒绝删除
  "不是本次会话创建的" venv。也就是说 README 原来那条"`install --force` → `inject` → `doctor`"
  的升级剧本在干净机器上第二次跑就失败。改成先 `pipx uninstall` 再 `pipx install`，
  等价且不依赖 pipx 版本。卸载只删 venv，`~/.voxos` 里的模型/声纹/日志一个不动。
- **安装脚本装成功了却报失败**：EXIT trap 里 `[ -n "$WORK" ]` 在本地 wheel 路径下返回 1，
  而 bash 会用 trap 里最后一条命令的退出码覆盖脚本退出码。加 `return 0` 钉住。

### 工程

- GitHub Actions 升到 `actions/checkout@v7`、`actions/setup-python@v7`。
  v4/v5 已被强制跑在 Node 24 上并持续告警，将来会变成硬错误。
- 发布门禁可显式豁免：`scripts/verify.py --release --allow-gap <关键字> --reason <理由>
  --until <日期>`。此前 GAP（中英混说 95%）永远关不掉，`make verify-release` 永久红，
  门禁等于没有；现在豁免必须留理由和到期日，到期自动失效。

## [0.1.13] - 2026-10-28

### 新增

- `scripts/install_smoke.sh`：临时 venv + 临时 HOME，在仓库外跑 6 项安装后断言。
  CI 的 package job 与 `make install-smoke` 跑同一份。
- `voxos --version`：以前报"缺少 command"。
- `doctor` 报出资源根 / 数据根 / 配置文件路径（含可写性）。

### 修复

- `__version__` 停在 0.1.10 而 pyproject 已是 0.1.12，两个 tag 发出去都没人察觉。

### 工程

- CI 矩阵改成 `plan` job 算出来：push 跑 4 个 job，推 tag 才补齐 macOS 3.11/3.12。
- release job 校验 tag 与 pyproject 版本一致，不一致直接退出。

## [0.1.12] - 2026-10-28

### 修复

- **装出来的包改不了配置**：设置页写回的是 `<venv>/share/voxos/...`，那是只读资源，
  `pipx upgrade` 还会整份替换。首次启动改为 seed 一份到 `~/.voxos/settings.toml`，
  出厂配置只当模板。

## [0.1.11] - 2026-10-28

### 修复

- **切 `stt.backend = sense_voice` 就报缺 `model.int8.onnx`**：`make_stt` 用 `cfg.get`
  取相对路径，相对的是当前工作目录；仓库根下跑正常，装完就坏。改用 `cfg.path()`。

## [0.1.10] - 2026-10-28

### 改进

- 模型下载失败时说明卡在哪一步、下一步怎么做，而不是抛一个 URLError。

## [0.1.9] - 2026-10-28

### 修复

- `voxos models` 一调就崩：`main.py` 里用了 `os` 却没 import。

## [0.1.8] - 2026-10-28

### 新增

- 模型下载进包（`voxos models`），首次安装不再需要手动放文件。
- 数据根与安装目录分离：模型、声纹、日志、状态落到 `~/.voxos/`，
  `pipx upgrade` 重建 venv 不会把它们一起删掉。

## [0.1.7] - 2026-10-28

### 修复

- 审计整改：修 CI 第一次跑暴露的三个真问题（构建产物、审计项口径、验收数字守卫）。
