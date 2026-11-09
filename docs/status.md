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
- 选择 cloud-hypervisor 后端时不执行宿主机任务函数
- 快照策略拒绝 vault，干净任务目录也返回未实现且不复制文件
- 令牌文件权限，以及权限被放宽后拒绝启动
- 源码中不存在 `shell=True` 或 `os.system`
- 内部 Git 仓库不继承外部签名、钩子、身份、属性或 `*.md` 排除规则；重复提交已存在的批准内容不会再报空的 `git command failed`。这是 Linux 上的回归，不是 Mac 实测
- 钉住的 Letta Code 0.34.45：`agents create --backend local --personality blank` 不需要账号或模型，会生成 `memfs/<agent-id>/memory` 的 Git 仓库；`memory tokens` 把根目录 Markdown 标为核心；批准后的 git 提交能被 `memory export` 读回，且新主题不进入核心列表。该测试由 `SSIOS_LETTA_UPSTREAM=1` 打开，默认 CI 不安装这个包

mock 测试使用的提供方名字就是 `mock`。Ollama 桩测试的名字表明对方不是真正的 Ollama 守护进程。

## 依赖外部集成

这些代码路径存在，但本环境没有把它们跑成端到端系统：

- 真实 Ollama 进程和真实 7B/8B 模型。适配器会发 `POST /api/chat`，本机没有安装 `ollama`
- 可打开的 `/dev/kvm`、`cloud-hypervisor`、`virtiofsd`、客户机内核和根磁盘。缺任何一项时预检失败
- 即使上述文件都存在，`execution_implemented` 仍是 false，因为仓库没有客户机代理协议，也没有测试过启动时延
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
