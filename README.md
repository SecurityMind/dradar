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

最简单的方式：去 [radar.codexradar.com](https://radar.codexradar.com) 用 GitHub 登录、在大表里点一个还没人做的格子认领——网站会直接给你一条粘贴命令，装 CLI、认证、开始跑这道题一次搞定。

也可以手动来：

```bash
# 一次性
uvx --from git+https://github.com/SecurityMind/dradar dradar login \
  --server https://api.codexradar.com --token <your-token> --tasks-root <deep-swe/tasks 的路径>
uvx --from git+https://github.com/SecurityMind/dradar dradar doctor   # 体检，按提示修复

# 日常
uvx --from git+https://github.com/SecurityMind/dradar dradar go       # 领题 → 跑 → 自动上传
uvx --from git+https://github.com/SecurityMind/dradar dradar resume   # 继续上次没跑完的
```

其他命令：`dradar status`（看自己的提交记录/积分/异常标记）、`dradar rename <新名字>`（改榜单显示名，积分不受影响）、`dradar link-github`（把账号和 GitHub 身份绑定，换机器能找回身份）、`dradar retry-upload`（补传因网络问题失败的提交）。

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
