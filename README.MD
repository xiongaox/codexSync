# codexSync

在两台个人电脑之间安全交接 Codex 本地状态的命令行工具。支持两种存储方式：传统的本地云盘目录同步，以及 AWS S3 / Cloudflare R2 的不可变快照。

> [!IMPORTANT]
> 这是冷同步工具：执行 `push`、`pull`、`sync` 或 `restore` 前必须完全关闭 Codex。当前真实环境主要验证 Windows 到 Windows；macOS（Apple Silicon）已有代码与 CI 覆盖，但尚缺少完整的真实双机验收。

## 它解决什么问题

- 在另一台电脑继续使用已有的 Codex 会话、Skills 和 Plugins。
- 覆盖前创建备份，避免直接破坏原有状态。
- 对两端同时修改的内容停止并报告冲突，而不是静默覆盖。

## 不做什么

- 不调用或修改 Codex API、二进制文件与运行时。
- 不迁移登录令牌、Cookies、`.env`、私钥或浏览器会话。
- 不检查云盘客户端状态、S3/R2 费用或剩余空间。
- 不提供实时同步；一次只有一台机器应处于活跃工作状态。

## 推荐方式：S3 / Cloudflare R2 快照

这是最简单的日常交接方式：源电脑发布一个不可变快照，目标电脑恢复最新快照。

```text
源电脑：关闭 Codex → cs push
目标电脑：关闭 Codex → cs pull → 打开 Codex
两端都有新内容：关闭 Codex → cs pull --merge → 处理冲突 → cs push
```

### 一次性安装

```powershell
python -m pip install -e .
```

安装后可使用短命令 `cs`。不使用短命令时，`codexsync` 与 `python -m codexsync` 也等价。

### 创建配置

```powershell
cs init-config
```

默认配置文件为：

```text
Windows: %USERPROFILE%\.codexsync\config.toml
macOS:   ~/.codexsync/config.toml
```

程序会优先读取当前目录的 `config.toml`；不存在时自动读取上述默认路径。仍可用 `-c <路径>` 指定其他配置文件。

### 配置 Cloudflare R2

在 `config.toml` 中填写（不要把 Access Key 或 Secret 写进此文件）：

```toml
[storage]
backend = "s3"

[s3]
bucket = "codexsync"
prefix = "codexsync"
endpoint_url = "https://ACCOUNT_ID.r2.cloudflarestorage.com"
region = "auto"
addressing_style = "path"
```

`endpoint_url` 在 Cloudflare R2 控制台的 **S3 API** 区域获取。R2 API Token 只需授予目标 bucket 的“对象读和写”权限。

凭证由 AWS SDK 标准凭证链提供，不进入 `config.toml`。例如当前 CMD 会话可设置：

```cmd
set "AWS_ACCESS_KEY_ID=你的AccessKeyID"
set "AWS_SECRET_ACCESS_KEY=你的SecretAccessKey"
set "AWS_DEFAULT_REGION=auto"
```

不要向聊天、日志、截图或 Git 仓库发送 Secret Access Key。

### 常用命令

```powershell
# 检查配置、S3 访问、文件写入权限和 Codex 进程状态
cs doctor

# 源电脑：创建 ZIP + SHA-256 manifest 并上传到 S3/R2
cs push

# 目标电脑：下载、校验并恢复最新快照；本地覆盖前自动备份
cs pull

# 两台机器都有新增内容时进行保守合并
cs pull --merge

# 查看历史快照，或恢复指定快照
cs list-snapshots
cs pull --snapshot <snapshot-id>
```

每次 `push` 都会创建新的不可变快照；上传 ZIP 和 manifest 校验通过后才更新远端 `latest` 指针。若远端快照比本机基线更新，`push` 会拒绝执行并要求先 `pull` 或 `pull --merge`。

`pull --merge` 会：

- 合并不同路径的文件；
- 对 `session_index.jsonl` 按完整行去重；
- 对同路径但内容不同的会话、Skill 或 Plugin 报告冲突并停止，不自动选择一方。

## 传统云盘目录同步

如果使用 OneDrive、Dropbox、Syncthing 或本地网络共享目录，可保持：

```toml
[storage]
backend = "filesystem"
```

然后执行：

```powershell
cs doctor
cs plan
cs sync --dry-run
cs sync --apply
```

传统模式通过 `paths.cloud_root_dir` 指定云盘同步目录，执行双向文件比较与同步。严格交接顺序是：关闭机器 A 的 Codex → 等云盘传播完成 → 在机器 B 关闭 Codex 后运行同步 → 同步完成才打开 Codex。

## 同步范围与排除规则

默认同步根目录：

```toml
[targets]
include_roots = [
  "sessions",
  "session_index.jsonl",
  "skills",
  "plugins",
]
```

默认排除锁、临时文件、缓存、`__pycache__` 和日志。可在 `[filters] exclude_globs` 中调整。认证和敏感信息不应加入同步范围。

`sync.session_mode = "last_date_only"` 时，只同步 `sessions` 下最新的日期目录；无法识别日期目录时会回退为同步全部 session。

## 备份、恢复与冲突

- 覆盖任何已有目标文件前，都会按 `[backup]` 创建备份。
- `backup.compression = "none"` 保存目录快照；`"zip"` 保存 ZIP 快照。
- 本地备份恢复：

```powershell
cs restore --apply
cs restore --from <snapshot-name> --apply
```

- 传统目录同步的 `conflict.policy`：
  - `manual_abort`：报告冲突并停止（默认）；
  - `prefer_cloud`：选择云端；
  - `prefer_local`：选择本地；
  - `prefer_newer_mtime`：选择修改时间较新的一方。

## 进程与安全策略

Windows 上检测到 `codex-windows-sandbox` 时，工具会直接停止；检测到仅有 `codex.exe` 时，默认要求用户确认后才会尝试关闭它。可以使用：

```powershell
cs --manual-terminate-confirmation sync --apply
cs --auto-terminate-without-confirmation sync --apply
```

macOS 上只要检测到 Codex 正在运行，就会停止操作。所有不确定状态均应以安全失败处理。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功或无变更 |
| `1` | 运行时错误 |
| `2` | 检测到需手动处理的冲突 |
| `3` | Codex 仍在运行 |
| `4` | 配置或命令参数错误 |
| `5` | 安全中止 |

`doctor` / `preflight` 在所有检查通过或仅有警告时返回 `0`；有失败项时返回 `5`。

## 日志与平台

- 日志等级：`DEBUG`、`INFO`、`WARNING`、`ERROR`。
- 日志格式：`text`、`json`、`logfmt`，支持按日期和大小滚动、保留与压缩归档。
- 当前 CI：`windows-latest`、`macos-latest`；Linux 运行时暂不在 MVP 范围内。

## 定时任务模板

仓库提供可编辑模板：

- Windows Task Scheduler：[scripts/scheduler/windows/README.MD](./scripts/scheduler/windows/README.MD)
- macOS LaunchAgent：[scripts/scheduler/macos/README.MD](./scripts/scheduler/macos/README.MD)

定时任务不会绕过冷同步要求。验证阶段始终先使用 dry-run。

## 开发

```powershell
python -m unittest discover -s tests -v
```

## 许可证

本项目采用双重许可：

- 开源：`GPL-3.0-or-later`，见 [LICENSE](./LICENSE)
- 商业许可：见 [COMMERCIAL_LICENSE.MD](./COMMERCIAL_LICENSE.MD)

贡献条款见 [CONTRIBUTING.MD](./CONTRIBUTING.MD) 与 [CLA.MD](./CLA.MD)。发布清单见 [docs/PUBLISHING.MD](./docs/PUBLISHING.MD)。
