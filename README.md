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
  --server https://api.codexradar.com --token <your-token> --tasks-root <deep-swe/tasks 的路径>
uvx --from git+https://github.com/SecurityMind/dradar dradar doctor   # 体检，按提示修复

# 日常
uvx --from git+https://github.com/SecurityMind/dradar dradar go       # 领题 → 跑 → 自动上传
uvx --from git+https://github.com/SecurityMind/dradar dradar resume   # 继续上次没跑完的
uvx --from git+https://github.com/SecurityMind/dradar dradar leases   # 查看当前占用：运行中 / 排队中
uvx --from git+https://github.com/SecurityMind/dradar dradar release  # 交互选择并释放不再准备跑的格子
```

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
- `dradar doctor` 会检查这些前置条件并给出针对性的修复建议（macOS/Linux/WSL2 都覆盖）

常见环境坑：OrbStack 首次启动需要打开一次 GUI 初始化；`uv` 会给 `.venv` 打上 macOS 隐藏标志，导致 Python 3.12 跳过 `.pth` 文件（`doctor` 会检测并提示 `chflags -R nohidden .venv` 修复）。

## 开发

```bash
uv venv
uv pip install -q --no-deps . --python .venv/bin/python   # 非 editable！改完源码要重跑这行
uv pip install -q pytest httpx --python .venv/bin/python
pytest tests/
```

**不要用 `-e`（editable）安装**——macOS 上 `uv` 建的 `.venv` 会被打上隐藏标志，Python 3.12 因此跳过 editable 安装依赖的 `.pth` 文件，导致 `import dradar` 直接失败（`chflags -R nohidden .venv` 能临时解决，但标志会莫名其妙又回来）。非 editable 安装把包文件真正拷进 `site-packages`，不依赖 `.pth`，不受这个坑影响——代价是每次改了 `src/` 下的代码，都要重新跑一遍上面的 `uv pip install` 这行，`pytest` 测的才是新代码，不然会静默测到旧代码。
