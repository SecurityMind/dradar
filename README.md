# dradar — DeepSWE 众测 CLI

社区志愿者用自己的 Codex/Claude 订阅额度跑 DeepSWE 基准任务，产出提交给服务端独立重跑判分、汇总上榜。这个仓库是**客户端**——运行在你自己机器上的部分。服务端（调度、判分、榜单）不在这个仓库里，是私有的。

## 它做什么

```
你的机器                                 服务端(独立仓库，私有)
┌────────────────────────────┐          ┌──────────────────────────────┐
│ dradar go / resume          │──领题──▶ │ 覆盖度优先调度 + 租约          │
│  └─ pier run (docker 沙箱,  │◀─指派──  │ /submissions 二次脱敏入库      │
│      无判分，出口白名单)      │          │  └─ grading worker:          │
│  └─ 脱敏 → 上传 patch +     │──提交──▶ │      独立重跑判分              │
│      trajectory + result    │          │ 榜单 / 大表 (公开可查)          │
└────────────────────────────┘          └──────────────────────────────┘
```

- **判分不信任客户端**：服务端用任务自带的 verifier 对回传的 patch 重新判分，客户端自报的结果一概不算数
- **你的订阅凭据全程留在你自己机器上**，从不上传到服务端——CLI 只是调用你本地已登录的 `codex`/`claude` CLI
- 上传前有敏感信息扫描，带密钥的补丁直接拒收

## 快速开始

最简单的方式：去 [deng.codexradar.com](https://deng.codexradar.com) 用 GitHub 登录、在大表里点一个还没人做的格子认领——网站会直接给你一条粘贴命令，装 CLI、认证、开始跑这道题一次搞定。

也可以手动来：

```bash
# 一次性
uvx --from git+https://github.com/SecurityMind/dradar dradar login \
  --server https://api.codexradar.com --token <your-token>
uvx --from git+https://github.com/SecurityMind/dradar dradar doctor   # 体检，按提示修复

# 日常
uvx --from git+https://github.com/SecurityMind/dradar dradar go       # 领题 → 跑 → 自动上传
uvx --from git+https://github.com/SecurityMind/dradar dradar resume   # 继续上次没跑完的
uvx --from git+https://github.com/SecurityMind/dradar dradar leases   # 查看当前占用：运行中 / 排队中
uvx --from git+https://github.com/SecurityMind/dradar dradar release  # 交互选择并释放不再准备跑的格子
```

一条命令并发执行多道题时，显式指定 worker 数；默认仍为 1，不会改变普通运行行为：

```bash
dradar go --auto 5 --workers 3       # 最多持有 5 道，同时运行 3 道
dradar resume --workers 3            # 用 3 个 worker 继续现有任务
dradar capacity                      # 查看本机保守推荐并发数
dradar resume --workers auto         # 按 Docker CPU/内存、磁盘和账号上限自动选择
```

父进程只认领一次任务，再由服务端原子地给各 worker 分题，不会让多个 worker 重复
执行同一道题。worker 共享本机 CPU、内存和模型额度；未传 `-y` 时会在启动前确认一次。
中断或部分 worker 启动失败时，父进程会统一停止已经启动的 worker，已完成上传和
checkpoint 均保留。`--parallel` 继续作为手工启动多个独立 CLI 进程时的底层选项，
使用 `--workers` 时不需要再传。

`--workers auto` 读取 Docker 引擎实际可用资源（Docker Desktop/OrbStack 可能只分到
宿主机的一部分），按每个任务至少 2 CPU、6 GiB 内存并预留构建峰值保守推荐。检测失败
时自动退回 1；普通用户自动推荐最多 4。手动 `--workers N` 的既有行为不变。

任务仓库默认克隆到 `~/.dradar/deep-swe/tasks`；如需放在其他位置，再传
`--tasks-root <路径>`。已有配置中的旧路径会继续使用，不会自动搬迁或重复克隆。

批量释放尚未开始的格子用 `dradar release --all`；指定格子用
`dradar release <assignment-id>`。正在本地运行的格子默认受到保护，只有确认本地
runner 已停止但状态仍卡住时才使用 `dradar release <assignment-id> --force`，避免
把仍在消耗额度的任务释放给别人重复运行。

其他命令：`dradar status`（看自己的提交记录/积分/异常标记和占用摘要）、`dradar rename <新名字>`（改榜单显示名，积分不受影响）、`dradar link-github`（把账号和 GitHub 身份绑定，换机器能找回身份）、`dradar retry-upload`（补传因网络问题失败的提交）。

## 中断续跑与本地清理

Codex 任务运行期间，Pier 每 30 秒把工作区差异、未跟踪文件、Codex session id、
运行阶段和心跳写入 `~/.dradar/work/jobs/` 下的私有 checkpoint。WebSocket/TLS
断开、模型容量不足、CLI 退出或机器重启后，下一次 `dradar go` / `resume` 会先恢复
checkpoint，再领取新格子。原 Codex session 不可用时会保留工作区，用已有进度摘要
启动新 session 继续。

```bash
dradar resume                         # 优先恢复所有未完成 checkpoint
dradar resume --assignment <id>       # 只恢复指定 assignment
dradar checkpoints                    # 查看状态、更新时间和磁盘占用
dradar checkpoint discard <id>        # 放弃 checkpoint 并重新开放格子
```

checkpoint 不保存账号 Token、assignment nonce 或 Codex `auth.json`；恢复时重新向
服务端鉴权。凭据形态的工作区内容会使 checkpoint 安全失效，session 中检测到凭据
则不持久化该 session，并降级为保留工作区的新会话恢复。

磁盘默认不会无限增长：提交成功或服务端确认已提交后立即删除该题的所有本地副本；
恢复产生新副本后删除旧副本；损坏、不兼容、超过 7 天或租约已失效的 checkpoint
会被标记无效、重新开放格子并回收。上传暂时失败时只保留可重试的最新任务目录；
只有显式传入 `--keep` 才保留成功任务的最终目录。

交互运行在成功上传后会询问是否立即删除本地任务文件，直接回车保持原有的
“成功后清理”默认行为；`-y` / `--parallel` 不会弹窗。随时运行
`dradar cleanup --dry-run` 预览可释放空间，或运行 `dradar cleanup` 一键清理。
清理命令会先向服务端确认当前租约，绝不删除仍在运行、可以恢复、等待上传或由
`--keep` 明确保留的任务；清理由 `--keep` 保护的目录需显式使用
`dradar cleanup --include-kept`。

### 持续自动补题（显式开启）

普通 `go` / `resume` 仍只运行用户已经认领的题，不会在后台继续领题。交互运行会在
启动时询问一次是否持续补题，默认回答为否。无人值守或并发运行必须显式给出硬上限：

```bash
dradar resume -y --refill --refill-to 5 \
  --quota-tier pro-5x --max-estimated-quota-pct 15
```

`--refill-to` 是持有/运行队列目标；预计额度上限是用户需要指定的主要停止条件，会按
`--quota-tier` 换算。接近上限时不再补充，让队列自然排空；缺少可靠估价的题不会被
自动领取。CLI 另有一个正常情况下不会触发的内部题数安全上限；高级用户仍可用
`--max-tasks` 把它设得更低。多个本机
`--parallel` worker 在同一个 `DRADAR_HOME` 下共用带文件锁的持久化计划，不会各自
重新计算额度或任务数。任何非正常提交都会停止补题，但不会释放已有租约或删除
checkpoint。

```bash
dradar refill status   # 查看共享计划、已预留题数和停止原因
dradar refill stop     # 立即停止继续领题，已有任务保持原样
```

自动补题继续使用网页“系统推荐”同一个 `/api/v1/suggest` 接口，不在 CLI 内维护第二套
推荐算法。当前共享上限以本机 `DRADAR_HOME` 为边界；不要在多台机器上分别创建补题
计划，否则每台机器会拥有自己的独立上限。

## 租约心跳与隐私

`dradar go` / `resume` 会为本次 CLI 进程建立一个轻量会话心跳：运行或上传时约
60 秒一次，准备、排队或暂停时约 120 秒一次。它按“会话”上报，不按认领的格子
数量上报；一次拿 10 个格子也仍然只有一条心跳。服务端还可以返回更慢的间隔，
以便在高峰期主动降流量。

心跳只包含 CLI 版本、粗粒度平台（macOS/Linux/WSL/Windows）、阶段、当前
assignment id、递增序号和进度计数；不会包含任务内容、prompt、trajectory、patch、
命令输出、主机名、用户名、硬件信息或订阅凭据。正常心跳只保留当前会话状态和
5 分钟聚合桶，不逐条写审计日志。

当前为影子观察阶段：服务端只记录“按候选规则本来会怀疑/释放”，**不会因为心跳
中断自动释放任何格子**。断网也不会终止正在运行的 Pier。你始终可以用
`dradar leases` 查看占用，用 `dradar release` / `release --all` 手动归还；运行中格子
仍受默认保护，需明确 `--force` 才能释放。

## 环境要求

- Docker（推荐 [OrbStack](https://orbstack.dev/)，macOS 上更轻量）
- 本地已登录 `codex` 或 `claude` CLI（订阅凭据留在本地，不经过 dradar）
- `dradar doctor` 会检查这些前置条件并给出针对性的修复建议（macOS/Linux/WSL2/Windows）

### 原生 Windows 候选支持

原生 Windows 需要 Docker Desktop 运行 **Linux containers**，并在 PowerShell
中安装可直接调用的 Codex CLI。IDE 扩展能登录不等于 `codex` 命令已在 `PATH` 中；
请以 `dradar doctor` 的实际检查结果为准。Codex CLI 的官方 PowerShell 安装命令是：

```powershell
irm https://chatgpt.com/codex/install.ps1 | iex
codex login
```

WSL2 仍然可用，但不限定 Ubuntu；Debian、OpenSUSE 等普通 WSL2 Linux 发行版也可以。
Docker Desktop 自己的 `docker-desktop` 是内部发行版，不能替代供用户安装并运行
DRadar 的普通 WSL2 发行版。

常见环境坑：OrbStack 首次启动需要打开一次 GUI 初始化；`uv` 会给 `.venv` 打上 macOS 隐藏标志，导致 Python 3.12 跳过 `.pth` 文件（`doctor` 会检测并提示 `chflags -R nohidden .venv` 修复）。

## 开发

```bash
uv venv
uv pip install -q --no-deps . --python .venv/bin/python   # 非 editable！改完源码要重跑这行
uv pip install -q pytest httpx --python .venv/bin/python
pytest tests/
```

**不要用 `-e`（editable）安装**——macOS 上 `uv` 建的 `.venv` 会被打上隐藏标志，Python 3.12 因此跳过 editable 安装依赖的 `.pth` 文件，导致 `import dradar` 直接失败（`chflags -R nohidden .venv` 能临时解决，但标志会莫名其妙又回来）。非 editable 安装把包文件真正拷进 `site-packages`，不依赖 `.pth`，不受这个坑影响——代价是每次改了 `src/` 下的代码，都要重新跑一遍上面的 `uv pip install` 这行，`pytest` 测的才是新代码，不然会静默测到旧代码。
