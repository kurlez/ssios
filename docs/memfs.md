# Letta MemFS 集成边界

默认模式 `letta-memfs-compat` 只复用公开的 Git Markdown 形状，方便没有 Letta CLI 时运行 mock 演示。它不是 Letta MemFS。

可选模式 `letta-memfs-upstream` 才调用上游。钉住的包是 `@letta-ai/letta-code@0.34.45`，完整性摘要写在 `third_party/letta-code.pin.json`。

## 上游现状

核对时间：2026-11-08。

| 来源 | 观察 |
| --- | --- |
| [MemFS 文档](https://docs.letta.com/letta-code/memfs) | 记忆是属于代理的 Git 仓库。根目录 Markdown 进入每轮上下文；带自己 `MEMORY.md` 的目录按需读取。除索引外的文件使用 `name` 和 `description` 前言。默认没有向量索引。本地代理把提交留在当前机器。 |
| [@letta-ai/letta-code 0.34.45](https://github.com/letta-ai/letta-code) | Apache-2.0。`package.json` 的 `bin` 是 `letta`。包体包含可执行代理、shell 和工具运行时。 |
| `letta-code` 导出的 `memory-confinement` | 这是给子进程套沙箱的启动器。上游注释写明该进程仍可以广泛读取宿主机。它不是可嵌入的 MemFS 存储 SDK。本仓库不调用它。 |
| [自托管文档](https://docs.letta.com/self-hosting) | `letta --backend local` 可以不登录，把状态放在 `~/.letta/lc-local-backend`。每个代理的 MemFS 在 `memfs/<agent-id>/memory`。代理工具运行在 harness 所在机器上。 |
| [Letta 服务器的 local MemFS](https://docs.letta.com/v1-sdk/docker) | `LETTA_MEMFS_SERVICE_URL=local` 提供 API 级记忆块。文档写明该模式下 Git HTTP 代理返回 501，Letta Code 的 clone/push 流程不可用。社区 Git sidecar 被标为非官方。 |

npm 上的 `memfs` 是另一个内存文件系统包。本项目不依赖它，也不依赖 `@letta-ai/letta-code` 或 `@letta-ai/letta-code-sdk`。

## 本仓库实际做了什么

控制面拥有一个普通 Git 仓库，目录形状与文档中的 `$MEMORY_DIR` 相同：

- 根目录 `MEMORY.md` 是没有前言的索引，属于核心记忆
- 其他根目录 Markdown 带 `name` 和 `description`，每次任务先加载
- `topics/` 和 `skills/` 不进入核心，只能按路径读取
- 搜索是大小写不敏感的关键字扫描，`search_mode=keyword`
- 批准后的修改用 `git commit` 固定下来，期望修订必须等于当前 HEAD
- 遗忘使用 `git rm`，来源记录改为墓碑；API 不再返回正文

经纪 API：

- `GET /v1/memory/core`
- `GET /v1/memory/file`
- `GET /v1/memory/search`
- `POST /v1/memory/propose`
- `POST /v1/memory/forget`
- 批准通过 `POST /v1/approvals/<id>/decide`，体内包含 `expected_revision`

提案先落在仓库外的 JSON 文件，信任值是 `untrusted`。批准前，核心读取和按路径读取都看不到提案正文。

`ssios memfs-info` 只打印上述钉住的版本和本地探测结果。默认不执行 `letta`。`--execute-version` 才会运行 `letta --version`，并设置 `LETTA_CODE_TELEM=0` 与 `DISABLE_AUTOUPDATER=1`。控制面启动时不会做这件事。

## 上游模式实际调用的命令

在空的 `HOME` 和单独的 `LETTA_LOCAL_BACKEND_DIR` 里，只允许这些参数：

- `letta --version`
- `letta --backend local agents create --personality blank`
- `letta --backend local memory tokens --format json`
- `letta --backend local memory export --agent ... --out ...`
- `letta --backend local memory status --agent ...`

`memory` 子命令的帮助写明：记忆由 Git 支持，提交使用 git。批准之后，SSIOS 只在该代理的 `memfs/<agent-id>/memory` 目录里 `git add` / `git commit`，并把 `core.hooksPath` 指到一个空目录，避免运行 Letta 安装的 `post-commit`（那个钩子会在配置了远端时后台 `git push`）。

核心文件列表来自 `memory tokens`。主题目录来自 `memory export` 里不在该列表中的 `MEMORY.md`。读取时，Git `HEAD` 的字节必须和同一次 export 一致。

明确拒绝的参数包括 `-p`、`--yolo`、`--permission-mode`、`--tools` 和 `--disable-memory-guard`。因此没有“无模型的完整代理回合”。上游本地创建代理本身不需要账号，也不需要调用模型；这已经在 0.34.45 上验证过。

## 没有做的事

- 没有运行 `letta -p` 或任何会让 harness 执行 shell 的回合
- 没有调用 `api.letta.com`
- 没有实现 Git smart HTTP sidecar
- 没有 QMD
- 没有把 `memory-confinement` 当作安全边界
- 兼容模式不会在上游模式失败时自动接替
