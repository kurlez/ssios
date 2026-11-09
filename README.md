# SSIOS

## microVM 还不能执行任务

Cloud Hypervisor 代码只生成启动参数并做预检。它没有启动过客户机，也不是生产执行环境。缺 KVM、`cloud-hypervisor`、`virtiofsd`、内核或磁盘时会失败关闭，并且不会改在宿主机上跑任务。`dev-guard` 只是进程内的工具白名单。

## 两种记忆模式

默认 `letta-memfs-compat` 是本仓库自己的 Git Markdown 兼容库，用来在没有 Letta CLI 时跑通 mock 演示。它不是 Letta MemFS。

可选 `letta-memfs-upstream` 调用钉住的 Letta Code `0.34.45`（Apache-2.0）本地后端。它只运行文档中的 JSON 子命令：`agents create`、`memory tokens`、`memory export`、`memory status`，然后在 Letta 创建的 `memfs/<agent-id>/memory` 仓库里做经过批准的 git 提交。不会运行 `letta -p`，也不会加 `--yolo`。CLI 缺失或版本不符时直接失败，不会退回兼容库。

控制面、审批、审计和项目存储在本机完成。开发档默认只绑定 `127.0.0.1`，模型是确定性 `mock`。

## 环境

- Python 3.11 或更新版本（本仓库在 Python 3.12 上测试）
- Git 2.x（MemFS 兼容库用它做版本提交）
- 不需要 pip 第三方包、云密钥、Docker 或 root

开发档默认只绑定 `127.0.0.1`，模型提供方是确定性的 `mock`，不访问网络，也不下载模型。

## 命令

```bash
make test      # 回归测试，不需要密钥
make demo      # mock 演示。自动选一个空闲端口，可与 make up 同时存在
make up        # 前台启动开发档，默认 127.0.0.1:8765，Ctrl-C 停止
make preflight # 打印 Cloud Hypervisor 预检。这不是启动虚拟机
make memfs-info
```

`make demo` 使用状态目录 `.ssios-demo`，不会删除已有的 `.ssios` 或 `.ssios-demo`。批准如果中途失败，直接再执行一次 `make demo`：它会接着处理仍在等待的审批，而不是另起一份会冲突的任务。若那份状态不想再动，可以换一个新目录，旧目录保持原样：

```bash
PYTHONPATH=src python3 -m ssios demo --state .ssios-demo-retry --config config/ssios.example.toml
```

上游 MemFS 冒烟（需要已安装钉住版本，且不会启动代理对话）：

```bash
npm install --prefix "$HOME/.ssios-letta" --no-fund --no-audit @letta-ai/letta-code@0.34.45
PYTHONPATH=src python3 -m ssios memfs-upstream-smoke \
  --state .ssios-upstream --letta-bin "$HOME/.ssios-letta/node_modules/.bin/letta"
```

完整性校验见 `third_party/letta-code.pin.json`。Node 需要满足该包的 engines 字段。不要把 `LETTA_API_KEY` 放进环境；这条命令会丢掉父进程里的该变量。

也可以直接调用：

```bash
PYTHONPATH=src python3 -m ssios demo --state .ssios-demo --config config/ssios.example.toml
PYTHONPATH=src python3 -m ssios health --state .ssios --config config/ssios.example.toml
```

`make up` 会在第一次运行时生成 `.ssios/secrets/human.token` 和 `worker.token`，权限为 `0600`。这两个文件已被 gitignore。人类控制令牌和工作者令牌不同。批准动作只能使用人类令牌。

演示会提交合成项目 `demo-alpha` 的摘要，等待人类批准，写入记忆并发布，然后重启进程再读回。审计日志只记录动作和载荷哈希。

## 连接本机 Ollama

mock 与 Ollama 是两条配置，没有自动回退。把样例复制出来再改，不要把真实选择写回被跟踪的样例文件：

```bash
cp config/ssios.example.toml .ssios/ollama.toml
```

把 `.ssios/ollama.toml` 里的 `provider` 改成 `ollama`，并按本机模型名修改 `ollama_model`。建议从 7B/8B 的量化模型开始，名称只是配置，不是写死的依赖。Ollama 默认监听 `127.0.0.1:11434`。首次 `ollama pull` 会下载模型；下载完成后推理可以离线进行。本程序不会自动拉取模型，也没有遥测。

```bash
PYTHONPATH=src python3 -m ssios up --profile dev --state .ssios --config .ssios/ollama.toml
```

模型不在或端口不通时，任务失败，错误码是 `llm_unavailable`。

Linux 客户机访问不到 macOS 宿主机的 `127.0.0.1`。默认不要把 Ollama 绑到 `0.0.0.0`。如果必须从客户机调用宿主机模型，只为该客户机地址做显式转发，并在防火墙上收紧来源。详见 [docs/macos-m4.md](docs/macos-m4.md)。

## 文档

- [架构与信任边界](docs/architecture.md)
- [MemFS 集成边界](docs/memfs.md)
- [实现状态](docs/status.md)
- [威胁模型](docs/security.md)
- [Mac mini M4 24GB](docs/macos-m4.md)
- [Ubuntu 与 Cloud Hypervisor](docs/deployment-ubuntu.md)
- [许可证](docs/licenses.md)
- 样例配置：[config/ssios.example.toml](config/ssios.example.toml)

## 许可

本仓库代码使用 MIT 许可证。Letta MemFS 的概念和 Letta Code 实现归 Letta 所有，上游许可证是 Apache-2.0。本仓库没有拷贝 Letta Code 源码。
