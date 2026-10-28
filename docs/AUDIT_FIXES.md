# 审计修复记录（0.1.6 → 0.1.7）

对应报告：`outputs/CODE_AUDIT-0.1.6-2026-10-28.md`（基线 `07bb17d`）。
原则：**每条都以行为测试为关闭依据**，不以文档勾选为依据；不能完整关闭的，写清剩余边界。

## 已关闭

| ID | 修复 | 守卫 |
| :--- | :--- | :--- |
| A01 | `SelfHealingSource` 进入设备源上下文（`__enter__`）再迭代；`close` 的 `stop` 失败不再跳过 `close` | `test_real_mic_source_is_started_inside_self_healing` |
| A02 | tmux 文本用 `shlex.join` 逐参数引用、`send-keys -l` 原样发送；检查创建/写入/提交返回码；委托会话重名时失败关闭 | `test_tmux_prompt_keeps_argument_boundaries` / `test_tmux_failure_is_reported_as_failure` |
| A03 | 确认按 request id 隔离，首答锁定，过期/重复回答拒绝；API 只接受真布尔并回传 id | `test_confirm_answer_only_unblocks_its_own_request` / `test_stale_confirm_id_cannot_approve_a_later_request` |
| A04 | 执行侧 Schema 校验：对象、必填、类型、enum、未知字段；坏 JSON 不再变成 `{}` 调用 | `test_invalid_enum_is_rejected_before_any_write` 等 4 条 |
| A05 | 记录「已执行副作用」，总结失败不再重走离线规划 | `test_llm_failure_after_a_tool_call_does_not_replay_offline` |
| A06 | 状态投递改非阻塞，满队列丢最旧；慢订阅不再阻塞生产者和健康订阅 | `test_full_subscriber_does_not_block_emit` |
| A07 | 快照正文与 `meta.json` 分路径；目标名为 `meta.json` 也能正确回滚 | `test_target_named_meta_json_survives_rollback` |
| A08 | 快照与工具共用 `strip()` 解析；新建文件可撤销；回滚前先备份当前版本 | `test_whitespace_path_is_snapshotted` / `test_new_file_can_be_undone` / `test_rollback_itself_leaves_a_snapshot` |
| A09 | 缺安全策略拒绝全部工具；未知角色白名单为空（不再 `*`） | `test_missing_policy_rejects_every_tool` / `test_unknown_role_is_not_wildcard` |
| A10 | 拒绝控制字符与 NaN/Inf；候选文本先 `tomllib` 解析再原子替换 | `test_control_characters_and_nan_are_rejected` / `test_invalid_value_never_reaches_the_file` |
| A11 | 保存/撤销同锁；备份按配置身份命名并保留 0600；临时文件 + `os.replace` | `test_two_configs_do_not_share_one_undo_slot` |
| A12 | Host allowlist（默认回环名）；Origin 只接受与请求 Host 相同且为 http | `test_untrusted_host_cannot_read_the_page` |
| A13 | 查询函数只返回数据，路由层统一发送一次 | `test_settings_get_sends_exactly_one_response` |
| A14 | 远端 STT 标 `remote=True`；候选态先过声纹，未确认不上传 | `test_remote_stt_never_sees_a_stranger` |
| A15 | 重载保留同一把执行锁；`_listen` 读取 `runtime.cfg`；EchoGate 接入配置衰减 | `test_reload_keeps_the_execution_lock` |
| A16 | 采集线程 → 识别线程 → 派发主循环，三段有界队列；断流重置句级状态；STT 异常不再杀死监听 | `test_remote_stt...`（含断流语义）；真实设备待验 |
| A17 | TTS 任务所有权：只有当前 owner 能清 `proc` 和结束回声门 | 代码路径 + 既有 barge 测试 |
| A18 | 录入时长 1–10 秒有限值；声纹先在副本计算，保存成功才提交，失败回滚 | `_enroll_now` 事务 |
| A20 | `flag()` 解析字符串；布尔环境键无论配置是否存在都按布尔；资源根支持安装布局；Silero 相对路径统一走 `cfg.path` | `test_env_false_is_false_even_without_a_config_key` |
| A21 | 页面缓存只存模板，令牌按请求注入 | `test_page_cache_never_reuses_another_context_token` |
| A23 | `make verify` 为回归模式；`make verify-release` 让 GAP/SKIP 失败；GAP 标签不再覆盖达标 | 代码 + 文档 |
| A24 | 短术语必须精确匹配；一个输出词只能认领一次；混说达标要求两套语料都 ≥95% | `verify.py` 逻辑 |
| A19 | 工作区路径 allowlist：`cfg.path_allowed()` 默认只放行代码目录/日志目录及其子目录，相对路径一律锚到代码目录；越界抛 `PermissionError`，不落快照、不执行 | `test_outside_workspace_path_is_refused` / `test_tool_exception_does_not_escape` |
| A25 | 审计 `ok` 不再靠前缀猜；请求体 1MB 上限与 30s 连接超时；文件头逐行读、笔记搜索先限量；SSE 订阅上限 16，超限 503 | `test_tool_failure_is_not_recorded_as_success` / `test_stream_returns_503_when_subscribers_are_exhausted` |
| A22 | 声明 `data-files`（config + web）、安装根回退；真实构建 wheel 并在空目录脱源安装冒烟 | 下方「wheel 脱源实测」 |
| A26 | 补 `LICENSE`（Apache-2.0 全文）、`SECURITY.md`、CI 矩阵与 wheel 冒烟；模型下载写 `.part`、校验大小/非零/SHA256 后原子替换；四个模型已填实测 SHA256 | `fetch_models.verify` + CI |
| A27 | 验收结论改为「18 通过 + 1 未达标」；删除不存在的 `--all`；收窄 README/REDLINES 的安全承诺；度量表同步 | `tests/test_docs.py` |

## 部分关闭（剩余边界写在这里）

- **A16**：线程解耦与断流重置已做；真实麦克风的溢出率、8 小时、热插拔/蓝牙/休眠仍未实测。
- **A19**：路径边界已收紧到「默认工作区 allowlist」，`workspace.allow_outside_roots = true` 是显式的放弃边界开关。仍属**进程内**约束：外部 CLI（tmux 里跑的东西）的副作用不受本进程管控，也不试图沙箱化。
- **A25**：订阅配额已加（`StatusBus.MAX_SUBSCRIBERS = 16`，超限返回 503 而不是挤掉老连接）；
  仍**未引入结构化 `ToolResult`**，结果仍以文本 + 审计布尔表达，调用方拿不到字段化的失败原因。
  每个 HTTP 连接一个线程的模型也没换（`ThreadingHTTPServer`），订阅配额只是给 SSE 这条路设了闸，
  不是通用的连接池。
- **A26**：四个模型的 SHA256 已按实际下载文件填入；**第三方模型许可证与来源清单仍需人工核对**，这不是代码能替人做的判断。

## wheel 脱源实测（0.1.7）

```bash
python -m build --wheel            # dist/voxos-0.1.7-py3-none-any.whl
python -m venv /tmp/voxos-smoke/venv
/tmp/voxos-smoke/venv/bin/pip install dist/voxos-0.1.7-py3-none-any.whl
cd /tmp && /tmp/voxos-smoke/venv/bin/voxos tools
```

观察到的结果：

- `voxos tools` 正常列出工具，不依赖源码树。
- 资源根解析为 `/private/tmp/voxos-smoke/share/voxos`（安装布局回退生效）。
- `chunk 512`、`redlines 11` 从安装目录读到。
- 页面返回 200、31131 字节、令牌已注入、无 `__VOXOS_TOKEN__` 残留。
- 错令牌访问 `/api/settings` 返回 401。

## 验证

```bash
make test            # 219 passed
make verify          # 回归模式：18 通过 + 1 未达标（混说术语召回，能力边界）
make verify-release  # 发布门禁：退出码 1（GAP 也算不通过），这是**预期**结果
```

`verify-release` 现在返回 1 不是回归——它拦住的是「真人混说 95% 尚未达标」这件事，
而这件事只能靠真人语料解决，不能靠改代码。门禁在说谎才可怕，失败是它在正常工作。

原审计探针 `outputs/audit_probes.py` 保留为**基线证据**：它按 0.1.6 的缺陷写断言，
修复后多数会变成 `PROBE_ERROR`，这是预期结果，不是回归失败。

## 装完真跑才暴露的一个（0.1.11）

| 现象 | 根因 | 修复 | 守卫 |
| :--- | :--- | :--- | :--- |
| `pipx install` 之后切到 `stt.backend = sense_voice` 就报「`.voxos/models/sense-voice` 里缺 model.int8.onnx」——可在仓库根跑同样的命令又是好的 | `make_stt` 用 `cfg.get("stt.model_dir")` 原样取相对路径，相对的是**当前工作目录**；`make_embedder` / `make_vad` 早就走 `cfg.path()` 了，只有 STT 这一处漏了 | `make_stt` 改 `cfg.path("stt.model_dir", ...)`；顺手把 `refresh_hotwords` 的 workspace 默认值也统一到 `cfg.path()` | `test_relative_model_dir_resolves_against_data_root` |

为什么之前的测试没拦住：跑源码时数据根就是仓库根，而人总是站在仓库根敲命令，
相对路径「碰巧」是对的。**只有装出来、在别处启动，才会掉进这个坑。**
这和 A19/A22/A26 是同一类问题——「源码布局下正常」不等于「装完正常」。
