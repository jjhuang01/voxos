# 变更记录

本文件是 Release 说明的唯一来源：`scripts/release_notes.py <版本>` 从这里抽出对应段落，
CI 的 release job 用它当 `gh release create --notes-file`。所以**发版前必须在这里写一段**，
否则 tag 打上去，Release 页面会是空的。

格式约定：`## [x.y.z] - YYYY-MM-DD`，一个版本一段，从新到旧。

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
