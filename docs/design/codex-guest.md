# 在 SSIOS 客户机里跑 Codex CLI

本文只是验证原型的软件架构，不是实现说明。事实核对日期是 2026-11-09。没有在本仓库里安装或运行 Codex，也没有把某个模型接到 Codex 上测过。标成「未核实」的句子不能当成已经试过。

## 要保住的边界

SSIOS 控制面留在宿主机。人类令牌、工作者令牌、审批（动作、载荷哈希、预期修订、nonce）、MemFS 写入和质量门都在宿主机。Gmail 和 Drive 的 OAuth 令牌也只在宿主机。客户机拿不到这些令牌，也拿不到真正的 Ollama 地址或 Google 的地址。模型输出、邮件正文、云端文件和 Codex 写出来的补丁、笔记、记忆提案都是不可信工件。由这些内容产生的记忆写入仍先过质量门，再由人批准。没有云端模型回退。客户机起不来就失败关闭，不改在宿主机上跑 Codex。本阶段不存在写、发送、共享或删除邮件和文件的工具，不是「先调用再等人批」。

两条运行路径不一样：

- Linux 上的 Cloud Hypervisor 不传 `--net`。这是硬隔离目标。客户机没有常规网络，只能走设计里选定的 vsock。
- macOS 上的 krunvm 0.2.7 没有关闭客户机网络的参数。它适合开发和对照，隔离弱于 Cloud Hypervisor。krunvm 的 TSI 仍可能让客户机里的进程自己去连网。设计不能把这条路径说成没有网络。

已有的 `guest_worker.py` 继续负责「读授权、写一次模型请求、写出摘要」。Codex 是另一种客户机代理，由配置 `[runtime] agent = "codex"` 选中。`agent` 缺省时仍走现在的 worker，不改变已在 Apple M4 上通过的 krunvm 摘要路径。

## 已核对的 Codex 事实

下列链接在 2026-11-09 打开过。Codex 变动很快，实现前要按当时的 tag 再对一次。

| 事项 | 结论 | 来源 |
| --- | --- | --- |
| 当时的最新稳定发布 | `rust-v0.167.0`，2026-11-06，不是预发布 | <https://github.com/openai/codex/releases/tag/rust-v0.167.0> |
| Linux 静态包 | 该发布同时提供 `codex-x86_64-unknown-linux-musl.tar.gz` 和 `codex-aarch64-unknown-linux-musl.tar.gz`，另有 `.zst` 和 `.sigstore` | 同上，GitHub release 资产列表 |
| 配置位置 | `CODEX_HOME`，默认 `~/.codex`。`config.toml` 在这里。命令行 `--config key=value` 的优先级高于文件 | <https://learn.chatgpt.com/docs/cli/reference>，以及仓库 `docs/config.md` |
| `--oss` | 使用本地开源模型提供方。具体是 `--local-provider`、配置里的 `oss_provider`，或在 LM Studio 与 Ollama 之间提示选择 | 同上 CLI 参考 |
| 内置 `ollama` 提供方 | 不能靠同名配置项覆盖。自定义端点要用**新的**提供方名字，并且不要同时用 `--oss --local-provider ollama`，否则会回到内置的 localhost 行为 | <https://github.com/openai/codex/issues/8240>，维护者 2026-06-12 关闭说明：换一个名字，或用 `CODEX_OSS_BASE_URL` |
| 线路协议 | 2026-11-09 的配置参考写明 `model_providers.<id>.wire_api` 只接受 `responses`，省略时也是它。`chat` 已被移除，留在文件里会导致整个配置加载失败 | <https://developers.openai.com/codex/config-reference>；<https://github.com/openai/codex/issues/41816> |
| 旧文档仍写 chat | 仓库里一份历史 `docs/config.md`（blob `88abbf58…`）仍把 `wire_api` 写成 `chat` 或 `responses`，并给过 `http://localhost:11434/v1` 的 Ollama 例子。以配置参考和 41816 为准，那份旧文过时 | 该 blob |
| 登录 | `model_providers.<id>.requires_openai_auth` 默认 false。设为 true 才走 OpenAI 认证。自定义提供方不要同时设置 `env_key` | 配置参考 |
| 非交互 | `codex exec`（短写 `codex e`）用于脚本。`--json` 输出按状态变化分行的 JSON。另有 `--output-schema` 和 `--output-last-message` | CLI 参考，`codex exec` 一节 |
| 沙箱 | `read-only`、`workspace-write`、`danger-full-access`。`codex exec` 默认只读。`--full-auto` 已不支持 | <https://learn.chatgpt.com/docs/non-interactive-mode>；<https://learn.chatgpt.com/docs/agent-approvals-security> |
| 审批策略 | `--ask-for-approval` 的取值是 `on-request` 或 `never`。这是 Codex 自己的暂停，不是 SSIOS 的人类批准。`--yolo` 会同时关掉沙箱和审批，本设计禁止 | 同上 |
| MCP | `[mcp_servers.<name>]` 支持 `command`/`args`/`env`（stdio）或 `url`（HTTP）。可限制 `enabled_tools`，也可用 `bearer_token_env_var` 指向环境变量名，而不是把令牌写进文件 | 历史 `docs/config.md` 的 `mcp_servers` 表；配置参考仍列出这些键 |
| 遥测 | 默认会向 OpenAI 发送用量与健康指标，不含文档所称的个人身份信息。`analytics.enabled = false` 关掉内置 Statsig。`otel.metrics_exporter` 默认跟着 analytics；要明确关掉应设为 `none`。OTel 日志导出默认关闭 | <https://learn.chatgpt.com/docs/config-file/config-advanced>；配置参考 |
| 更新检查与网页搜索 | `check_for_update_on_startup` 默认为真。`web_search` 默认 `cached`，会用 OpenAI 维护的索引；`live` 会真正访问外网。`--yolo` 时网页搜索会变成 `live` | 配置参考 |

未核实，实现前必须用钉住的二进制再测：

- Ollama 0.40.15（本仓库以前用过的版本）是否实现 Codex 要的 Responses API。Codex 侧 PR <https://github.com/openai/codex/pull/8798> 说内置 Ollama 会按 Ollama 版本在 Responses 和 chat 之间选择，但本设计不使用内置提供方。
- `codex exec --json` 每一行的字段名。文档只说是分行的 JSON 事件，没有在这里钉死 schema。
- 关掉 `analytics` 和 `web_search` 之后，进程是否还会在有网时连到 OpenAI。无网客户机上连不上就失败；有网的 krunvm 上这仍是残余风险。
- musl 包在客户机里的动态依赖。发布名是 `unknown-linux-musl`，通常可在只有证书和 CA 的根文件系统里跑，但是否完全静态没有在这里用 `ldd` 看过。
- 发布资产的 SHA256。实现时从该 tag 的校验文件读取，本文不写未核对的哈希。

## 已核对的外部连接事实

下列页面在 2026-11-09 打开过。没有在本仓库里创建 Google Cloud 项目，也没有用真实账号跑过授权。

| 事项 | 结论 | 来源 |
| --- | --- | --- |
| Gmail 只读范围 | `https://www.googleapis.com/auth/gmail.readonly`：查看邮件和设置。`https://www.googleapis.com/auth/gmail.metadata`：只看标签和信头，不含正文。两者都列在受限范围里 | <https://developers.google.com/workspace/gmail/api/auth/scopes> |
| 更宽的 Gmail 范围 | `gmail.modify`、`gmail.compose`、`gmail.send`、`https://mail.google.com/` 能改、发或删除。本阶段不申请 | 同上 |
| Drive 范围 | `https://www.googleapis.com/auth/drive.readonly`：查看并下载全部文件，受限。`https://www.googleapis.com/auth/drive.metadata.readonly`：只看元数据，也是受限。`https://www.googleapis.com/auth/drive.file`：非敏感，只能看、改、建、删「本应用打开或创建的文件」 | <https://developers.google.com/workspace/drive/api/guides/api-specific-auth> |
| 没有按文件夹的 OAuth 范围 | 官方范围表里没有「某个文件夹」这种范围。`drive.file` 选中一个文件夹并不等于能列出里面的文件。文件夹限制由 SSIOS 的任务授权再切一刀，不能靠 OAuth 范围本身 | 同上；Picker 与 `drive.file` 的限制见该页对 per-file 的说明 |
| 桌面应用回环 | 桌面客户端用 `http://127.0.0.1:<端口>` 或 `http://[::1]:<端口>` 收授权码。装好的应用每次都会返回 refresh token | <https://developers.google.com/identity/protocols/oauth2/native-app> |
| 撤销 | 应用可 `POST https://oauth2.googleapis.com/revoke`，参数是 access token 或 refresh token。人也可以在 Google 账号的第三方访问里撤销 | 同上 |
| 测试模式 | 外部用户、发布状态为 Testing 时，只有名单里的测试用户能授权，上限 100。会看到测试警告。除了仅请求姓名、邮箱、头像的情况外，refresh token 七天后过期 | <https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification>；<https://support.google.com/cloud/answer/10311615> |
| 受限范围核实 | 公开应用申请受限范围要做额外核实。若经第三方服务器访问或保存这类数据，还要年度安全评估。个人使用、开发、测试、暂存可以不送审，但仍受测试人数和七天令牌的限制 | 受限范围核实页，Exceptions 一节。页脚写着 2026-10-30 UTC |
| 官方远程 MCP | Gmail `https://gmailmcp.googleapis.com/mcp/v1`，Drive `https://drivemcp.googleapis.com/mcp/v1`。文档标成 Developer Preview，要加入 Workspace Developer Preview Program。配置示例同时要求 `gmail.readonly` 和 `gmail.compose`，以及 `drive.readonly` 和 `drive.file`。示例里有 `gmail.create_draft` | <https://developers.google.com/workspace/guides/configure-mcp-servers>，页脚 2026-11-06 UTC；Gmail 与 Drive 各自的 configure-mcp-server 页，页脚 2026-11-05 UTC |

未核实：

- 官方 MCP 的完整工具名单。文档写了读、建草稿、改标签、建文件，但没有在这里把工具表逐项拉下来。
- `drive.file` 配合 Picker 时，用户一次点选的文件是否足以覆盖「读一个已知文件夹」。社区讨论说选文件夹不等于能读其中文件。本设计不把这条当已证实的能力。
- `google-api-python-client` 当前的传递依赖数量。没有在本仓库安装它。

## 组件

```mermaid
flowchart TB
  human[人]
  cp[宿主机控制面]
  secrets[secrets 目录 0600]
  conn[只读连接器]
  google[Gmail 与 Drive 的 REST]
  proxy[宿主机模型代理]
  ollama[本机 Ollama 127.0.0.1]
  mem[MemFS 与审批]
  kvm[Cloud Hypervisor 无网络]
  krun[krunvm 可能仍有 TSI]
  guest[客户机]
  relay[客户机环回转发 127.0.0.1]
  codex[codex exec]
  ws[工作区副本]
  mcpg[客户机本地 MCP]
  vsock[vsock 或 krunvm 的 vsock 到 Unix 套接字]

  human --> cp
  cp --> secrets
  secrets --> conn
  conn --> google
  cp --> mem
  cp --> kvm
  cp --> krun
  kvm --> guest
  krun --> guest
  guest --> relay
  relay --> vsock
  vsock --> proxy
  vsock --> conn
  proxy --> ollama
  codex --> relay
  codex --> ws
  codex --> mcpg
  mcpg -. 只访问工作区副本 .-> ws
  conn -. 裁过的不可信正文 .-> vsock
  guest -. 补丁与提案文件 .-> cp
  cp --> human
```

Cloud Hypervisor 是要验收的隔离。krunvm 只作为 Mac 上的开发后端画在旁边。

## 一次任务的数据流

1. 人用现有的人类令牌发起任务。控制面检查身份、项目和输入修订，和现在一样。
2. 宿主机把**已经允许阅读的正文**复制到本次任务目录的 `input/`。不复制令牌、不复制 MemFS 的 `.git`、不写 Ollama 的真实 URL。
3. 宿主机准备 `scratch/`，写入只在客户机里使用的 Codex 配置。`base_url` 固定为 `http://127.0.0.1:9` 这一类环回地址，端口由实现钉死。配置里的提供方名字用 `ssios`，不用 `ollama`。
4. 启动客户机。Cloud Hypervisor 不添加网卡。krunvm 按它自己的方式启动，并在文档和状态里标明网络未关闭。
5. 客户机里的转发进程监听 `127.0.0.1`。它只把字节转到 vsock（Cloud Hypervisor）或 krunvm 0.2.7 已有的 `--vsock 端口:宿主机 Unix 套接字`。转发进程不解析提示词，也不持有人类令牌。
6. `codex exec` 把模型请求发到环回地址。宿主机代理检查这条连接来自 vsock，而不是来自宿主机上的任意进程，然后按策略调用本机 Ollama，再把 Responses 流送回去。
7. Codex 在客户机里用自带的 shell。工作目录只是本次复制出来的工作区。它可以改这个副本。
8. 读 Gmail 或 Drive 时，客户机只调用宿主机公布的只读工具名。请求经同一条 vsock 到连接器。连接器用 `<state>/secrets/` 里的令牌访问 Google，按本次任务授权裁剪，再把标成不可信的正文送回。令牌不出宿主机。
9. 写 MemFS 或发布仍然不在客户机里执行。客户机只能提交记忆提案。控制面把它变成现有的审批单。邮件和文件内容不能自己变成批准。
10. 客户机退出后，宿主机只收集约定目录里的工件：`scratch/last-message.md`、`scratch/events.jsonl`、`scratch/patch.diff`、`scratch/proposals/*.md`。这些文件不可信。任务目录里从连接器复制来的正文随任务结束删除。
11. 质量门先看工件。通过之后才生成审批。人用人类令牌批准后，宿主机才写入 MemFS 或发布。客户机此时已经销毁。
12. 销毁客户机。krunvm 仍删除本次任务的虚拟机定义；镜像缓存可以留下，这和现在的 `vm-prepare` 一样。

任一步失败都停在失败状态。不自动改用 dev-guard，也不改用云模型。

## 宿主机模型代理

Codex 只会发 HTTP。客户机没有网卡时，它够不到宿主机的 `127.0.0.1:11434`。比较过三条路：

| 通道 | 做法 | 取舍 |
| --- | --- | --- |
| virtio-fs 上的请求文件 | 和现在的 `request.json` / `response.json` 一样 | Codex 不会读写这些文件。还得在客户机里再写一个 HTTP 服务器，把 HTTP 拆成文件。流式 Responses 会很别扭 |
| 串口 | 把 HTTP 帧塞进 virtio 串口 | 没有现成的 HTTP 客户端支持，调试和背压都差 |
| vsock 加客户机环回转发 | Codex 连 `http://127.0.0.1:端口`；转发进程把它送进 vsock；宿主机在 vsock 上提供 Responses API | Codex 不用改。Cloud Hypervisor 可以不加网卡。krunvm 0.2.7 的 `start --vsock` 已能把客户机的 AF_VSOCK 接到宿主机 Unix 套接字 |

选定第三种。

转发进程是客户机镜像里的一个小程序，只做 TCP 和 vsock 的字节拷贝，限制连接数和单请求大小。它不认识 JSON。宿主机代理才解析 Responses 请求。

宿主机代理对每个请求做这些事：

- 只接受来自本次任务 vsock 的连接。宿主机上的其他进程即使猜到路径也不能拿它当开放端口。
- 核对模型名在任务配置的允许列表里。不在列表里就拒绝，不转发。
- 去掉或拒绝任何 `Authorization` 里看起来像 SSIOS 令牌的值。客户机配置里的占位密钥是固定的短字符串 `ssios-guest`，只用于满足客户端要带一个头的情况。它不是人类令牌，也不能调用控制面。
- 记录请求字节数、响应字节数、耗时和模型名。不记录完整提示词到任务 API。完整正文只留在权限为 `0600` 的诊断文件，并且跑和现在一样的脱敏。
- 强制本次任务的输出上限和墙钟预算。超出就断开，任务失败。
- 只把请求转给配置里的本机 Ollama。Ollama 不在、返回错误或协议对不上时，任务失败。不改连别的主机。

代理不实现网页搜索，也不把 `web_search` 工具补上。客户机配置把 `web_search` 设为 `disabled`。

Ollama 是否提供 `/v1/responses` 未核实。若钉住的 Ollama 只有 chat，原型停在 M0 的模拟代理，直到换一个已经核实支持 Responses 的本机版本。不为此去接云端。

## 客户机里的 Codex 配置

配置由宿主机写进任务的 `CODEX_HOME`，再只读挂进客户机。示意：

```toml
model_provider = "ssios"
model = "qwen2.5-coder:7b"
check_for_update_on_startup = false
web_search = "disabled"

[analytics]
enabled = false

[feedback]
enabled = false

[otel]
metrics_exporter = "none"

[model_providers.ssios]
name = "SSIOS host proxy"
base_url = "http://127.0.0.1:9/v1"
wire_api = "responses"
requires_openai_auth = false

[mcp_servers.guest_fs]
command = "ssios-mcp-guest"
args = ["--root", "/workspace"]
enabled_tools = ["read_file", "list_dir"]

[mcp_servers.host_broker]
command = "ssios-mcp-host"
args = ["--vsock"]
enabled_tools = ["drive.search", "drive.read_file", "gmail.search", "gmail.get_message", "memory_propose"]
```

不使用 `--oss`。提供方名字不是 `ollama`，避免内置提供方把 URL 改回 `localhost:11434`。

启动命令的形状是：

```text
codex exec --json --sandbox workspace-write --ask-for-approval never \
  --ignore-user-config -C /workspace \
  --output-last-message /scratch/last-message.md \
  "任务说明"
```

`--ask-for-approval never` 只表示 Codex 不再向一个不存在的终端提问。沙箱仍是 `workspace-write`。禁止 `--yolo` 和 `danger-full-access`。SSIOS 的批准不靠这个开关。

`--ignore-user-config` 避免读到镜像或宿主目录里意外留下的配置。实际配置目录由 `CODEX_HOME` 指向本次任务的只读配置。这一点要在钉住的版本上确认 `--ignore-user-config` 是否仍加载 `CODEX_HOME`。未核实之前，M0 的验收包含「客户机里除任务配置外没有别的 config.toml」。

## 工具

两类工具，不能混。

客户机本地：

- Codex 自带的 shell 和补丁。工作目录是 `/workspace`，对应宿主机上本次复制的目录。沙箱是 `workspace-write`。
- 只读 MCP `guest_fs`，同样只看见这个目录。
- 这些调用不经过 SSIOS 审批。它们改不到 MemFS，也读不到宿主机的秘密。质量门只看离开客户机的工件。

宿主经纪：

- MCP 服务器 `host_broker` 的二进制在客户机里，传输仍走 vsock。它不持有 OAuth 令牌，也不在客户机里执行记忆写入或 Google 调用。
- 只读工具是 `drive.search`、`drive.read_file`、`gmail.search`、`gmail.get_message`。它们受任务授权、预算和审计约束，见下一节。单次读取不再另开一张 SSIOS 审批，因为授权在任务开始前已经由人给定。没有授权就拒绝。
- `memory_propose` 把客户机交来的 Markdown 提案交给现有的 `memory.commit` 审批。不新增批准语义。人批准之前，MemFS 不变。
- 工具表里没有发送、回复、改标签、建草稿、上传、改名、共享、删除。调用未知名字直接拒绝。拒绝 shell、拒绝发布到客户机看不见的路径、拒绝带着令牌的参数。

Codex 自己的审批提示如果在 `never` 下变成拒绝，任务失败并留下 JSONL。不把这种拒绝自动换成 SSIOS 批准，也不为了让它通过而去掉沙箱。

## 宿主经纪的外部连接

这一阶段只读 Gmail 和 Google Drive。客户机没有网卡，也看不到 OAuth 客户端、refresh token 或 access token。它看到的是四个工具名。真正的 HTTPS 只发生在宿主机连接器里。

### 为什么不用现成 MCP，而用自己的小适配器

比较过三条路：

| 做法 | 结果 |
| --- | --- |
| Google 官方远程 MCP | 文档仍是 Developer Preview。配置示例把 `gmail.compose` 和 `drive.file` 与只读范围放在一起，示例工具包含 `gmail.create_draft`。服务器在 Google 侧，本仓库不能删掉写工具，也不能审计它的实现。OAuth 客户端还要勾成给 AI 代理用。不选 |
| 第三方本地 MCP 服务器 | 常见实现是 Node，依赖多，工具表通常含发送、改标签或删除。钉死并审完一整棵依赖树，超出这个原型。不选 |
| 宿主机上的小适配器，直接打官方 REST | 调用面由我们列出来。只用 Python 标准库的 `http.server` 做 `127.0.0.1` 回环，用 `urllib` 打 Gmail 和 Drive。不引入 `google-api-python-client`。官方客户端会带上发现文档和一串传递依赖，原型阶段审不过来。若以后某个分页或导出格式用标准库写不清，再单独评估一个钉死版本，并写进锁文件。默认不加 |

客户机里的 `ssios-mcp-host` 只是把工具调用送到 vsock。宿主机连接器才是 MCP 工具的实现。Codex 的 `enabled_tools` 只列上面四个名字加 `memory_propose`。多出来的名字在宿主机再拒一次，防止客户机配置被换成更宽的列表。

### 授权

人在宿主机上执行一次性的安装应用流程。浏览器打开 Google 的授权页，授权码回到本机临时监听的 `http://127.0.0.1:<随机端口>`。监听只在这次命令期间存在，不对外网卡。

范围按用途分开，不要一次要齐：

- 只要信头和标签：`https://www.googleapis.com/auth/gmail.metadata`。这时 `gmail.get_message` 不能返回正文。
- 要读正文：才申请 `https://www.googleapis.com/auth/gmail.readonly`。它的官方说明包含「邮件和设置」。适配器不提供读设置、改过滤器的工具。
- 要在人预先点选的文件上读内容，且能接受「不能搜索整个云端硬盘」：`https://www.googleapis.com/auth/drive.file`。这个范围在官方分类里非敏感，但官方描述包含对这些文件的修改和删除。适配器不发出写请求。
- 要按文件夹搜索已有文件：`https://www.googleapis.com/auth/drive.readonly`。这是受限范围。没有更窄的「这个文件夹」OAuth 范围。文件夹 id 只写在 SSIOS 任务授权里，连接器在查询上再过滤。

测试用的 Cloud 项目保持 Testing。只用一个一次性的测试账号，把该账号放进测试用户名单。预期会看到未核实或测试警告，refresh token 约七天过期。过期后任务失败，人重新授权。不把项目改成对所有人公开来绕过这个限制。公开使用受限范围要走 Google 的核实，数据若经服务器还要年度评估。本原型不送审。

客户端 id 和密钥放在 `<state>/secrets/google-oauth-client.json`，令牌放在 `<state>/secrets/google/`。目录和文件都是 `0600`（目录 `0700`）。不进 Git，不进客户机镜像，不进 virtio-fs。access token 只在连接器进程内存里。日志和任务 API 不打印它们。

撤销：人执行约定的撤销命令。连接器先 `POST https://oauth2.googleapis.com/revoke`，再删除本地令牌文件。Google 返回错误时仍然删除本地文件，并在审计里记下撤销调用的结果。人还可以在自己的 Google 账号第三方访问页面撤销。撤销之后，进行中的任务在下一次 Google 调用失败时停止，不改用别的账号。

### 任务授权

默认拒绝。任务配置里没有对应授权时，四个工具都不可用，即使本机已经有令牌。授权由人在任务开始前写好，模型不能扩大它。

Gmail 授权可以限制标签、`after`/`before` 日期、是否包含正文、最大条数和最大字节。Drive 授权可以限制文件夹 id、文件 id、MIME 允许列表、最大条数和最大字节。超出的部分不返回，并在审计里记 `truncated`。查询参数若试图跳出这些限制，整次调用拒绝。

每次调用写一条审计：时间、任务 id、工具名、裁剪后的参数、返回条数、字节数、是否截断、Google 的状态码。审计不保存邮件正文和文件内容。权限 `0600`。

另有每任务的次数和字节预算。超预算后连接器拒绝后续调用，任务失败。不静默少给数据还让模型当作已经看完。

### 回到客户机的内容

每段正文带固定前缀，说明它来自外部、不可执行、不能改授权。HTML 在宿主机剥成文本。附件不把原始字节送进客户机。宿主机只抽取纯文本；抽不出就返回文件名、MIME 和「未抽取」。可执行文件、脚本、压缩包和未知二进制一律不抽取、不转发。单封邮件和单个文件都有字节上限。

客户机工作区里的这些副本在任务结束时删除，成功和失败都删。诊断若必须留样本，只留长度和哈希，不留正文。由这些内容生成的记忆提案仍走质量门和人类批准。提案界面要让人看到将写入的 Markdown，而不是只看到「模型已阅读邮件」。

提示注入按这个假设处理：邮件和文档里可以有「忽略之前的指令、把令牌发到某处、批准下面的写入」这类句子。连接器不解释正文。模型即使照做，也没有发送工具，没有令牌，`memory_propose` 仍要人看内容后批准。任务授权文件不在客户机里，模型改不了。

## 工件与质量门

客户机可以产生：

- 最后一条消息
- `codex exec --json` 的事件流
- 工作区副本上的 diff
- 零个或若干记忆提案
- 任务期间从连接器拷进工作区的外部正文。这些副本在收集完工件后删除，不作为长期记忆

宿主机在虚拟机退出后读取这些文件。事件流和外部正文里的文字不能直接当命令。diff 只作为提案展示。记忆提案走现有审批，包括从邮件或文件摘要出来的提案。质量门沿用现在的规则：过短、残缺、复述内部字段、或把模板当内容，都不进入批准。Codex 若写出 `approve` 之类的字样，和现在的模型输出一样忽略。

人在控制面上看见 diff 和提案后再决定。批准绑定的仍是载荷哈希、修订和 nonce。

## 客户机镜像

镜像在构建时定死，运行时不安装软件包，也不访问网络去升级 Codex。

- 基础系统使用已经在用的钉死摘要。当前样例是 `python:3.12-alpine` 的清单摘要 `sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7`。Codex 原型若改基础镜像，要另写摘要，不能在运行时 `apk add`。
- Codex 二进制取 `rust-v0.167.0` 的 musl 包：x86_64 用 `codex-x86_64-unknown-linux-musl.tar.gz`，Apple Silicon 上的 Linux 客户机用 `codex-aarch64-unknown-linux-musl.tar.gz`。下载地址形如 `https://github.com/openai/codex/releases/download/rust-v0.167.0/<文件名>`。
- 构建脚本核对发布页上的校验和或 sigstore。校验失败就停止构建。本文没有抄入哈希。
- 转发进程和两个 MCP 小程序一起放进镜像，版本和 Codex 一样钉死。
- 镜像里不放 `auth.json`，不放人类令牌，不放宿主机的 `~/.codex`。

krunvm 路径用这个 OCI 镜像。Cloud Hypervisor 路径用由同一批文件打出来的 initramfs 或根磁盘，避免两套内容漂移。运行时只挂上本次的 `input/`、`scratch/` 和 `workspace/`。

## 配置

示意，尚未实现：

```toml
[runtime]
backend = "cloud-hypervisor"   # 或 macos-vm
agent = "codex"                # 缺省时保持现在的摘要 worker
guest_image = "docker.io/library/python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7"
macos_vm_volume = "/Volumes/krunvm"
guest_prepare_timeout_seconds = 600

[agent.codex]
model = "qwen2.5-coder:7b"
max_output_tokens = 4096
max_request_bytes = 200000
max_tool_rounds = 8
task_timeout_seconds = 180

[connectors]
enabled = []

[connectors.gmail]
scope = "https://www.googleapis.com/auth/gmail.metadata"

[connectors.drive]
scope = "https://www.googleapis.com/auth/drive.readonly"

[connectors.budgets]
max_calls = 20
max_bytes = 200000
```

`enabled` 为空时，客户机里的四个外部工具名不出现。改成 `["drive"]` 或 `["gmail"]` 才注册对应工具。Gmail 要正文时，人把 scope 改成 `https://www.googleapis.com/auth/gmail.readonly` 并重新授权。不要同时申请 metadata 和 readonly。Drive 若改用 `https://www.googleapis.com/auth/drive.file`，搜索只能落在人在宿主机上点选过的文件 id，任务授权里的文件夹过滤仍然生效，但选不中的文件读不到。

单次任务的收窄写在该任务的授权里，不写进上面的全局配置。例如只允许某个文件夹 id、某些标签、一段日期、更小的 `max_bytes`。授权比全局预算更严的那一侧生效。模型提交的参数不能放宽授权。

`macos_vm_volume` 只用于 `vm-doctor` 的只读报告，默认 `/Volumes/krunvm`，与 krunvm 0.2.7 提示的默认挂载点相同。卷未挂载或 `diskutil info` 看不出 Case-sensitive APFS 时，医生报告失败原因，但不因此阻止一次已经能跑的 krunvm 任务。医生不执行 `hdiutil`，也不执行会创建或附加卷的 `diskutil`。

外接磁盘上的稀疏镜像由人自己做，例如：

```bash
hdiutil create -size 60g -type SPARSE -fs "Case-sensitive APFS" -volname krunvm /path/to/external/krunvm.sparseimage
hdiutil attach /path/to/external/krunvm.sparseimage
```

`volname` 为 `krunvm` 时，系统把它挂到 `/Volumes/krunvm`。路径里不要写具体的人或盘名。krunvm 第一次若还没有 `storage_volume`，由人在终端里回答 `/Volumes/krunvm`。SSIOS 不代替回答。

## 失败时怎么停

| 情况 | 行为 |
| --- | --- |
| 客户机或 Codex 二进制起不来 | 任务失败，`isolated` 仍为 false。不在宿主机上启动 Codex |
| 镜像拉取超过准备超时 | 失败，诊断里有 `krunvm create` 的退出码和输出尾部。不把这次失败当成客户机已经隔离运行 |
| 转发进程或 vsock 断开 | 失败。不改把 `base_url` 指到真的 Ollama |
| 代理看到不允许的模型、过大的请求，或 Ollama 错误 | 失败。不换模型、不换主机 |
| Codex 非零退出，或没有约定工件 | 失败。已有的部分 diff 留在任务目录供人看，不自动入库 |
| 质量门拒绝 | 失败，不创建批准 |
| 人拒绝或审批过期 | 与现在相同：过期的单子保持过期，不延长、不重用 nonce |
| krunvm 有网且 Codex 仍试图外连 | 任务可以继续，但状态里必须写明后端是 krunvm、网络未关闭。这不是隔离成功 |
| 任务没有外部授权，或参数超出授权 | 该次工具调用拒绝。不改用更宽的范围重试 |
| Google 返回 401，或测试模式令牌已过七天 | 任务失败。不自动打开浏览器，不换账号 |
| 撤销命令删不掉远端授权 | 本地令牌文件仍然删除。审计记下远端结果。之后的调用按没有令牌失败 |
| 连接器超字节或次数预算 | 任务失败，并在审计里记截断或拒绝。不把部分结果说成完整结果 |

诊断文件权限 `0600`，任务 API 只给短的 `error_detail`。两者都要脱敏。

## 威胁

| 威胁 | 挡住了什么 | 剩什么 |
| --- | --- | --- |
| 客户机读人类令牌 | 令牌不进镜像、不进挂载、不进代理允许的头 | 若宿主机把家目录或状态根目录挂进去，这个保证就没了。实现不得这样做 |
| 客户机直接打 Ollama 或云 | Cloud Hypervisor 无网卡；配置里的 URL 只是环回转发 | krunvm 关不掉网络。恶意或被注入的 Codex 仍可能自己拨号。分析数据默认也会外发，必须在配置里关掉，并且无网才算真正挡住 |
| 提示注入 | 笔记是不可信输入。宿主工具仍要人批准 | 模型可能在工作区副本里乱改，或诱导宿主工具的参数。批准界面必须给人看 diff 和参数，不能只给一句「模型说可以」 |
| 模型不会用工具 | 失败关闭，不假装完成 | 小模型经常发不合 Responses 要求的工具调用。这会让 M1 的验收失败，不能靠放宽解析去接受 |
| Codex 沙箱被 `--yolo` 关掉 | 设计禁止这个旗标 | 镜像或配置若被换掉，宿主机仍只收集约定目录。但客户机内的进程就不再受 Codex 沙箱约束 |
| 转发进程被换成隧道 | 镜像在构建时钉死 | 运行时若允许客户机写镜像自己的二进制，这条就不成立。挂载必须让镜像只读 |
| 审批重放 | 沿用现有的哈希、修订和 nonce | 新代码若自己实现一套批准，就会绕开它。记忆写入必须调用现有审批，而不是在 Codex 对话里认 `yes` |
| 客户机拿到 Google 令牌 | 令牌只在 `<state>/secrets/`，vsock 上的工具响应不含 `Authorization` 和 refresh token | 若有人把 secrets 目录挂进客户机，这条就破了。实现和测试都要确认挂载列表 |
| 邮件或文件里的提示注入 | 没有写、发、共享、删除工具。正文标成不可信。记忆写入仍要人看内容 | 模型仍可能把注入句子写进提案。批准界面若只显示「建议记一笔」而不显示正文，人会看漏 |
| 只读范围被换成可写范围 | 适配器不注册写方法，未知工具名拒绝。申请范围与配置不一致时拒绝启动连接器 | `drive.file` 的官方描述含修改和删除。令牌若被拿到客户机之外的程序里，对方仍可能写。所以令牌不能离开宿主机 |
| 官方或第三方 MCP 自带写工具 | 本阶段不连接这些服务器 | 以后若改用远程 MCP，必须先证明工具表里没有写操作。当前文档不能提供这个证明 |
| 测试模式令牌过期或被撤销 | 调用失败关闭，不降级成把人的浏览器会话转给客户机 | 七天到期是预期行为。人要重新授权 |
| 外部正文留在磁盘上 | 任务结束删除客户机副本。长期审计只记元数据 | 诊断文件若误写入正文，就违反最小化。测试要扫诊断目录 |

## 24GB M4 上的模型

统一内存要同时放 macOS、krunvm 客户机和模型。下面只按公开的体量做选择，没有在这台机器上用 Codex 跑过。

- 原型优先 `qwen2.5-coder:7b` 的量化版。权重大约数 GB，给客户机留出内存后仍比较宽裕。工具调用是否稳，未核实。
- `qwen3` 的 8B 一带可以作为对照。思考模型会吃输出预算，宿主机代理要保留现在的长度上限。
- `gpt-oss:20b` 更接近 Codex 文档里的 `--oss` 例子，但 20B 的 Q4 权重大约十多 GB，再加上上下文，24GB 机器上会和系统、客户机抢内存。只能作为可选配置，不能当默认。
- 14B 以上的 coder 变体同样可能把机器顶满。默认不选。

M1 的验收用 7B 档。若工具调用不合格，结论是「这个模型不够」，不是去接更大的云模型。

## 里程碑

每一级都要有命令输出、任务 JSON 和诊断文件。口头说明不算通过。krunvm 上的通过不能用来宣称 Cloud Hypervisor 已通过。

### M0：客户机里的 `codex exec` 加模拟代理

验收：

- Cloud Hypervisor 客户机里跑钉住的 `codex exec --json`，`--sandbox workspace-write`，没有 `--yolo`。
- 客户机配置里的 `base_url` 是环回地址。抓包或代理日志能看见请求只到 vsock 上的模拟 Responses 服务。
- 模拟服务不访问网络。Codex 退出码为 0，`last-message.md` 和 JSONL 出现在宿主机的 scratch。
- 进程环境和工作区里搜不到人类令牌。
- `analytics.enabled = false` 写在生成的配置里。无网卡客户机上不存在到外网的套接字。此项在 krunvm 上改为记录「有套接字也算失败」还是「只记录」必须在 M0 写进测试；默认按失败，因为这条路径不宣称隔离。

### M1：代理接到本机 Ollama

验收：

- 宿主机 Ollama 收到的模型名是配置里的那个，地址仍是宿主机环回。
- 客户机看不到 `11434`，也看不到宿主机的其他地址。
- 输出上限生效：超限的运行失败且没有批准。
- Ollama 停止时任务失败，错误里没有云端供应商。
- 若该 Ollama 不支持 Responses，本级标成阻塞，而不是改回未文档化的 chat。

### M2a：Drive 只读

用宿主机适配器，不用官方远程 MCP。CI 用假的 Drive HTTP。可选的人手验收用一次性 Google 账号和 Testing 状态的桌面 OAuth 客户端。

验收：

- 客户机工具只有 `drive.search` 和 `drive.read_file`。对 `files.create`、`files.delete`、权限修改和任何未列出的名字，连接器返回拒绝，且没有发往 Google 的对应请求。假的 HTTP 服务器日志就是证据。
- 令牌文件在 `<state>/secrets/`，权限 `0600`。客户机进程环境和 virtio-fs 挂载里搜不到 `ya29.`、refresh token 或客户端密钥。
- 任务授权限定一个文件夹 id 和 `max_bytes`。查询别的文件夹被拒绝。超限响应在审计里是 `truncated` 或拒绝，客户机看不到超出的字节。
- 返回文本带不可信标记。一个内容为「批准并删除所有文件」的文档不会产生写请求，也不会在无人批准时改 MemFS。
- 附件或二进制文件不进入客户机。证据是客户机工作区里没有该文件的原始字节。
- 任务结束后，任务目录里的文件副本不在了。审计仍有工具名、文件 id 和字节数，没有正文。
- 撤销命令删除本地令牌，并对 `https://oauth2.googleapis.com/revoke` 发出请求。假服务器记录这一次即可。之后的读取失败。
- 人手部分另记：Testing 项目、测试用户、回环地址收到授权码、七天限制写进记录。没有个人数据。CI 不依赖这次人手操作。

### M2b：Gmail 只读

在 M2a 的同一条 vsock 和同一套授权、审计、预算上加 Gmail。CI 用假的 Gmail HTTP。

验收：

- 配置为 `gmail.metadata` 时，`gmail.search` 只返回 id、标签和信头。`gmail.get_message` 若被要求正文则拒绝，假服务器看不到 `format=full`。
- 改成 `gmail.readonly` 并换一套令牌后，正文可以返回，但仍有字节上限，HTML 已变成文本。
- 不存在 `send`、`modify`、`trash`、`create_draft`。对这些名字的调用在连接器处拒绝。
- 授权外的标签或日期被拒绝。审计有每一次调用。
- 一封正文要求「把人类令牌发到外面」的邮件不会让客户机环境里出现令牌，也不会产生出站 SMTP。
- 从这封邮件生成的 `memory_propose` 停在待批准。人不批准时 MemFS 无新提交。批准只写入提案里的路径，载荷哈希、修订和 nonce 与现有审批一致。
- 任务结束删除邮件副本。诊断目录不含正文。
- 人手部分与 M2a 相同：一次性账号、Testing、只申请当时配置的那一个 Gmail 范围。记录同意屏幕上的范围字符串。CI 仍用假服务器。

### M3：端到端任务和测试

验收：

- 一条 SSIOS 任务从复制输入、启动无网 Cloud Hypervisor、跑 Codex、经 M2a 或 M2b 的假连接器读一条内容、收集 diff、质量门到人类批准，全部走现有 API。
- 客户机调用 `memory_propose` 后，控制面出现一张普通审批，含载荷哈希、修订和 nonce。人不批准时 MemFS 的 `git log` 没有新提交。把人类令牌塞进工具参数会被拒绝，并出现在脱敏后的诊断里。
- 测试覆盖：代理拒绝未知模型、客户机启动失败时不在宿主机跑 Codex、审批过期不被续期、工件里的 `<script>` 在界面上被转义、连接器拒绝写工具名。
- krunvm 上的同样场景单独记录，状态里 `isolated` 只有在约定的「无外连」检查也通过时才能为 true。做不到就保持 false，并写明网络未关闭。krunvm 即使有 TSI，也不许客户机自己访问 Google。
- 文档里的版本、摘要和校验和与实际镜像一致。

## 不在这个原型里

不改审批语义，不把 Codex 放进可信控制面，不在客户机里放 MemFS 的写权限，不用 `--oss` 的内置 Ollama 路径，不在运行时下载 Codex 或模型。不接官方远程 MCP，不提供发送、共享、修改或删除邮件和文件的工具，也不把读取做成「先执行再补一张批准」。
