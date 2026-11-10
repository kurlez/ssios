# 实现状态

状态分三类。命令构造和预检通过，不等于 microVM 已经跑起来。

## 已测试

在 Linux x86_64、Python 3.12、Git 2.43 上由 `make test` 覆盖：

- mock 开发档的健康检查、任务、核心记忆、按需主题、暂存、人类批准、发布、重启后读取
- 未认证请求、错误令牌、工作者令牌不能批准、跨项目拒绝、未授权导出路径
- 审批篡改、重放、过期、载荷字节被改、暂存冻结
- 路径穿越、符号链接、导出清单哈希、来源不明的 Git 文件
- 记忆和发布的 CAS，包括并发批准只有一个修订成功
- 遗忘后工作树和搜索不再返回正文，`git log -p` 仍保留该短语
- 核心令牌预算截断
- 任务超时、人类取消、输出长度上限
- 模型 `approve` 字段被忽略
- shell 工具被拒绝，实际拉起的进程只有 Git，没有 shell
- Ollama 适配器对环回桩的 `/api/chat` 请求，以及连不上 `127.0.0.1:9` 时的 `llm_unavailable`
- 生产档 CLI 在创建令牌之前以失败关闭退出
- 选择 `cloud-hypervisor` 或 `macos-vm` 后端时，创建任务会失败关闭，不在宿主机执行。`macos-vm` 只检查 `krunvm` 是否在 PATH 上，本机没有启动过客户机
- `ssios gc --dry-run` 只列出 `upstream-letta/exports/<agent>/` 下的一次性导出副本。真正删除需要再输入 `yes`，并且拒绝删除 `backend/memfs` 里的 Git 记忆
- 本机 `/ui` 页面：登录后会话 cookie 是 HttpOnly 且 SameSite=Strict，每次 POST 校验 CSRF，批准按钮带上同一组载荷哈希、修订和 bound hash。笔记里的 `<script>` 会转义。人类令牌不写入会话文件
- 快照策略拒绝 vault，干净任务目录也返回未实现且不复制文件
- 令牌文件权限，以及权限被放宽后拒绝启动
- 源码中不存在 `shell=True` 或 `os.system`
- 内部 Git 仓库不继承外部签名、钩子、身份、属性或 `*.md` 排除规则；重复提交已存在的批准内容不会再报空的 `git command failed`。这是 Linux 上的回归，不是 Mac 实测
- 钉住的 Letta Code 0.34.45：`agents create --backend local --personality blank` 不需要账号或模型，会生成 `memfs/<agent-id>/memory` 的 Git 仓库；`memory tokens` 把根目录 Markdown 标为核心；批准后的 git 提交能被 `memory export` 读回，且新主题不进入核心列表。该测试由 `SSIOS_LETTA_UPSTREAM=1` 打开，默认 CI 不安装这个包
- `ssios summary` 用相对 `--state` 连接已经运行的服务。子进程测试覆盖 `yes`、`no`、EOF，以及一次无效输出后的重试会新建任务并保留失败记录。模型 `<think>` 和 `message.thinking` 里的 `approve` 不会被解析成命令。Ollama 适配器会发送 JSON schema，并在服务拒绝 schema 时只再试一次 `format: "json"`。这些交互用的是环回 HTTP 桩。`ssios ingest` 把 md/txt 笔记经审批写入 `topics/<project>/`。`summary --topic` 只摘要这些正文。思考模型先要一段正文，JSON 只在校验通过时采用。失败的原文写在状态目录的 `diagnostics/<task_id>.json`（0600），不会自动批准。Linux 上用官方 Ollama 0.40.15 测过公开的 `qwen2.5:0.5b`、`qwen3:0.6b` 和 `deepseek-r1:1.5b`。没有在这里跑过 Mac 上的 `deepseek-r1:7b`。设置 `SSIOS_LETTA_UPSTREAM=1` 时，同一条命令会把主题提交进钉住的 Letta 0.34.45 本地 MemFS，且该主题不进入核心

mock 测试使用的提供方名字就是 `mock`。Ollama 桩测试的名字表明对方不是真正的 Ollama 守护进程。

## 依赖外部集成

这些代码路径存在，但本环境没有把它们跑成端到端系统：

- 真实 Ollama 进程和真实 7B/8B 模型。适配器会发 `POST /api/chat`。写这份状态时，这台 Linux 机器的 `127.0.0.1:11434` 没有服务，所以没有声称跑过 `deepseek-r1:7b`
- 默认测试不下载客户机内核。样例配置的 `kernel_path` 为空时，`cloud-hypervisor` 任务仍然失败关闭，不会改在宿主机上跑
- 这台机器上把 `/dev/kvm` 打开给当前用户之后，用 Cloud Hypervisor v54.0 和 cloud-hypervisor 发布的 Linux 7.2.8 内核（`ch-release-v7.2.8-20261009` 的 `bzImage-x86_64`）启动了 initramfs。`echo ok` 的墙钟是 898 毫秒。带 virtio-fs 输入和 scratch、没有 `--net` 的摘要 worker 用了 1576 毫秒，任务进入 `awaiting_approval`，`isolated` 为 true。模型调用是宿主机上的 mock。krunvm 没有安装，所以没有启动 macOS 那条路径
- `[runtime] agent = "codex"` 在同一台 Cloud Hypervisor 上跑了钉住的 `codex-cli 0.167.0` musl 二进制。宿主机先核对 `third_party/codex.pin.json` 里的 SHA256，客户机不下载。客户机只有 `lo`，`ip addr` 配上 `127.0.0.1/8` 后，环回中继经 vsock 打到宿主机的模拟 Responses 服务。没有 `--net`。`/sys/class/net` 只有 `lo`，连 `1.1.1.1:443` 失败。一次 `POST /v1/responses` 被代理记下，任务进入 `awaiting_approval`，`isolated` 为 true，`boot_ms` 为 7155。最后一条消息是模拟摘要，没有写入 MemFS。`codex` 的标准错误是空的。这不是 Mac 上的结果。`scripts/macos-codex-smoke.sh` 只在 Apple Silicon 上核对 aarch64 包，不启动客户机，也不把那次核对说成隔离成功
- `macos-vz` 用 vfkit 0.6.4 生成没有 `virtio-net` 的命令，并把 vsock 接到现有的宿主机代理。aarch64 内核钉在 `ch-release-v7.2.8-20261009` 的 `Image-arm64`。用户在 M4 上跑 `e1dad45` 时，准备成功，但 `/var/folders/...` 下的 AF_UNIX 路径超过 104 字节，`bind` 抛了 `internal`，当时没有诊断文件。套接字改到 `/tmp/ssios-<随机>`，超长路径失败关闭为 `socket_path_too_long`，未捕获异常会写入 `diagnostics/<task>.json` 和 `error_detail`。这台 Linux 机器没有启动 Virtualization.framework，Mac 上的 `isolated` 仍未成立
- 同一台机器上的 Ollama 0.40.15 提供 `/v1/responses`。`SSIOS_OLLAMA_LIVE=1` 且 `SSIOS_VM_LIVE=1` 时，客户机里的 Codex 用 `qwen2.5-coder:7b` 调用了三次 `exec_command`。工作区里写出了 `marker.txt`。写 `/opt/ssios-outside` 得到只读文件系统，连 `1.1.1.1:443` 得到 `Operation not permitted`。任务进入 `awaiting_approval`，`isolated` 为 true，`boot_ms` 为 43363。代理日志只有字节数和 SHA256。`deepseek-r1:7b` 在 200 个输出 token 内只产生推理摘要，没有工具调用。`qwen2.5-coder:1.5b` 的工具 JSON 不合格。krunvm 没有启动
- Apple M4、macOS、krunvm 0.2.7：镜像还没缓存时，`scripts/macos-vm-smoke.sh` 大约 31 秒后失败，`isolated` 为 false，`error_code` 是 `microvm_fail_closed`。用户先执行 `krunvm create python:3.12-alpine` 之后，同一条冒烟在 `2166525` 通过，结果是 `awaiting_approval`，`runtime_backend` 为 `macos-vm`，`isolated` 为 true，`boot_ms` 为 3513。没有个人数据。这台 Linux 机器没有 krunvm，没有复现那次启动
- Letta Code 0.34.45 的本地后端。探测函数可选，默认不执行

## 计划中

- Ubuntu 上的真实 microVM 快速启动和单次使用的干净预热池
- 每个导出一个 virtiofsd 进程的实际挂载
- 客户机网络经纪。现在默认拒绝，没有连接器
- 快照恢复。需要先做身份和密钥重置，当前直接拒绝带 vault、记忆或令牌的快照
- 启动信任链、TPM 密封的 vault
- 可选的机密计算 / TEE 剖面
- 静态数据加密。代码只做权限位，不做 LUKS 或 FileVault
- QMD 或 MemFS Search 的语义索引
- 图形界面、消息和采购连接器
