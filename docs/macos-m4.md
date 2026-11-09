# Mac mini M4，24GB 统一内存

目标机器是 Apple M4、24GB 统一内存。macOS 版本未知。本仓库的测试发生在 Linux x86_64 云环境，没有在这台 Mac 上运行，也没有测量模型速度。

## 规划起点

这些数字只用于留出内存，不是基准：

- Linux 客户机从大约 6GB 内存、2 个 vCPU 开始，里面跑控制面和记忆库
- 宿主机 Ollama 同时只放一个 7B 或 8B 的 Q4 量化模型。这类权重文件常见大约 4–6GB。实际占用随上下文变长而上升
- 24GB 里还要留给 macOS、客户机和模型。先不要再开第二个大模型

请用当时的 Lima、UTM 或 Apple Virtualization 文档核对客户机产品是否支持你的 macOS 版本。不要假设客户机里有可用的嵌套 KVM，也不要假设 GPU 能透传给 Linux。

## 推荐摆法

1. 在 macOS 上安装 Ollama，保持默认的 `127.0.0.1:11434`
2. 需要模型时手动 `ollama pull`。这一步会下载数据。之后的推理可以不访问网络
3. Linux 客户机或本机终端跑 SSIOS 开发档。开发档不需要 KVM
4. 生产 microVM 剖面需要 Linux 上的 `/dev/kvm`。macOS 本身没有这个设备。客户机里有没有它，要在选定的虚拟化产品上单独验证

SSIOS 配置里的 Ollama 地址必须是环回地址。程序会拒绝其他主机名。

## 客户机访问宿主机模型

客户机里的 `127.0.0.1` 是客户机自己，不是 Mac。默认不要把 Ollama 改成 `0.0.0.0`。

如果确实要让客户机调用宿主机模型，只做窄转发，例如只把客户机的一个端口转到宿主机的 `127.0.0.1:11434`，并让防火墙只接受该客户机地址。转发关掉后，开发档的 mock 模式仍然可以独立工作。本仓库不附带会放宽防火墙或绑定所有网卡的脚本。

## 演示如果在批准后报 git_failed

不要删除 `.ssios` 或 `.ssios-demo`。更新后再执行 `make demo`。

已在 Linux 上复现这次 `git rev-parse` 失败：状态目录是相对路径 `.ssios-demo`，仓库目录本身存在，但 `git -C` 仍用相对路径，而 Git 子进程的 cwd 是 `.ssios-demo/git-home`，于是报 `cannot change to '.ssios-demo/memory/demo-alpha/repo'`。修复是在启动时把状态目录收成绝对路径。若记忆仓库真的不存在、而审批又引用了旧修订，程序会停下且不重建仓库、不改审批。该复现没有在 Mac 上再跑。

## 服务已经起来之后

`ollama` 能回答，只说明本机模型进程在听。要跑通一次任务，用和正在运行的服务相同的状态目录和配置。不要再启动第二个服务，也不要删除 `.ssios-memfs`。

先在跑着 `ssios up` 的终端按 Ctrl-C，更新到当前分支后再启动那一个进程：

```bash
git fetch origin
git checkout cursor/ssios-local-prototype-6121
git pull
PYTHONPATH=src python3 -m ssios up --profile dev --state .ssios-memfs --config .ssios/memfs.toml
```

另一个终端：

```bash
PYTHONPATH=src python3 -m ssios ingest examples/notes \
  --project local-summary --state .ssios-memfs --config .ssios/memfs.toml
PYTHONPATH=src python3 -m ssios summary \
  --project local-summary --topic local-summary \
  --state .ssios-memfs --config .ssios/memfs.toml
```

`ingest` 只接受 md 和 txt，拒绝符号链接和看起来像密钥的文本。输入 `yes` 之后才写入记忆。`summary` 只读 `topics/local-summary/` 里的笔记，不把 Letta 的空模板当成正文。批准后的新摘要仍写到 `topics/fixtures/last-summary.md`，用新的 git 提交覆盖，不删除旧历史。

它只对合成项目 `local-summary` 提出摘要。看完拟发布的文本和记忆差异后，输入 `yes` 批准，或输入 `no` 拒绝。不输入就结束时，审批保持待处理。DeepSeek 的 `message.thinking`、`<think>` 内容和 JSON 里的 `approve` 都不会变成批准。

在 `.ssios/memfs.toml` 的 `[llm]` 里可以加上这些项，然后用原来的参数重启服务。不删状态目录，也不重新拉取模型：

```toml
max_output_tokens = 1024
num_ctx = 8192
max_note_chars = 6000
summary_repair_retries = 1
```

思考模型的摘要默认要一段正文，而不是 JSON。若仍然失败，输出里会有诊断文件路径。分类可以用：

```bash
PYTHONPATH=src python3 -m ssios diagnose <task_id> --state .ssios-memfs --config .ssios/memfs.toml
```

原文在 `.ssios-memfs/diagnostics/<task_id>.json`，权限是 0600，不会进入 git，也不会被自动批准。

如果摘要失败，旧任务会保留。截断、空答案和复述内部字段的文本不会进入批准。直接再执行同一条 `ssios summary`，它会新建任务。已经写进记忆的坏摘要留在 git 历史里；下一份获得 `yes` 的摘要会用新的提交覆盖 `topics/fixtures/last-summary.md`。要先区分失败原因，可以运行：

```bash
PYTHONPATH=src python3 -m ssios ollama-probe --state .ssios-memfs --config .ssios/memfs.toml
```

这条命令走开发档控制面。它不是 microVM，也没有把“模型端口通了”当成任务成功。

## 本环境没有覆盖的内容

- 没有 macOS 版本号，因此没有验证某个虚拟化产品的兼容矩阵
- 没有 Apple Silicon 上的 Cloud Hypervisor 启动
- 没有 virtiofs 在 M4 上的挂载测试
- 没有 FileVault 操作步骤的自动化。磁盘加密仍是系统设置里的前提
- 没有在这台 Mac 上复测 `ssios summary`。Linux 上的对应测试使用 mock 或 HTTP 桩，不代表 `deepseek-r1:7b` 已经在这里跑通
