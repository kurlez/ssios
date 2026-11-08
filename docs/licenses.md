# 许可证与依赖

## 本仓库

MIT License。版权所有者为 Kent Liu，见仓库根目录的 `LICENSE`。

运行时 Python 依赖为空。`pyproject.toml` 的 `dependencies` 是空列表。测试使用标准库 `unittest`。CI 只安装 Python 3.12，不读取仓库密钥。

## 系统依赖

| 组件 | 何时需要 | 许可证 |
| --- | --- | --- |
| Python 3.11+ | 始终 | PSF License |
| Git | 记忆库提交 | GPL-2.0。以独立进程调用，没有把 Git 源码链进本仓库 |
| Ollama | 只有把提供方改成 `ollama` 时 | 见 Ollama 项目自身的许可证。本仓库不附带模型权重 |
| Cloud Hypervisor | 只有将来真正启动 microVM 时 | Apache-2.0 与 BSD-3-Clause，以该项目仓库为准 |
| virtiofsd | 同上 | Apache-2.0 与 BSD-3-Clause，以该项目仓库为准 |

这些系统组件都不是本仓库的 vendored 副本。

## Letta

Letta MemFS 的文档和 Letta Code 实现属于 Letta，上游仓库是 <https://github.com/letta-ai/letta-code>，许可证 Apache-2.0。可选上游模式钉住 `@letta-ai/letta-code` 0.34.45。npm 完整性摘要：

`sha512-IhB4vDuNebew2bKbAGYJ3zROPG6kLD2rPhMHXOSSxjExDFCTLAN74d5/4Sn7K8d3yV8n2mZT5uWVcA4FbN+yJw==`

本仓库没有复制 Letta Code 的源码、提示词或模型。安装发生在状态目录或用户指定的前缀里，不进入 SSIOS 的默认依赖。`NOTICE` 说明了语义来源。兼容库是 SSIOS 自己的 Python 代码，MIT 许可，不能把它再描述成 Letta 的发行版。
