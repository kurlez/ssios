# SSIOS

本地个人 Agent OS 的可运行原型。控制面、审批、审计和项目存储都在本机完成。记忆层按 Letta MemFS 的 Git Markdown 语义实现，模式名是 `letta-memfs-compat`。它不嵌入 Letta Code，也不使用 npm 包 `memfs`。

这不是已经加固的生产操作系统。`dev-guard` 是进程内工具白名单，不是 microVM。Cloud Hypervisor 适配器只做预检和命令构造；真正启动客户机的路径会失败关闭，不会退回在宿主机上执行任务。

## 环境

- Python 3.11 或更新版本（本仓库在 Python 3.12 上测试）
- Git 2.x（MemFS 兼容库用它做版本提交）
- 不需要 pip 第三方包、云密钥、Docker 或 root

开发档默认只绑定 `127.0.0.1`，模型提供方是确定性的 `mock`，不访问网络，也不下载模型。

## 命令

```bash
make test      # 回归测试，不需要密钥
make demo      # 启动 mock 开发档，跑完合成项目摘要后退出
make up        # 前台启动开发档，Ctrl-C 停止
make preflight # 打印 Cloud Hypervisor 预检，预期失败关闭
make memfs-info
```

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
