# macOS 上没有网卡的 Codex 客户机

`agent = "codex"` 在 Apple Silicon 上用 `macos-vz`，不走 krunvm。摘要 worker 仍用 `macos-vm`。这篇说明核对的是 2026-11-09 的上游文档。Linux 云环境不能启动 Virtualization.framework，所以这里没有 Mac 启动结果。

## 为什么是 vfkit

| 方案 | 没有网卡 | vsock | virtio-fs | 安装 | 不选的原因 |
| --- | --- | --- | --- | --- | --- |
| vfkit 0.6.4 | 不写 `--device virtio-net` 就没有网卡。设备是逐个声明的 | `--device virtio-vsock,port=…,socketURL=…,listen`，宿主机是 Unix 套接字 | `--device virtio-fs,sharedDir=…,mountTag=…` | `brew install vfkit`。Apache-2.0。签名过的发布物带 `com.apple.security.virtualization` | 选定 |
| Apple `container` 0.6.0 的 `--network none` | 例子里 `ip addr` 只剩 `lo` | 文档没有把 vsock 暴露成我们能接的宿主机套接字 | 容器根文件系统，不是这次的 virtio-fs 副本 | `brew` 不一定是主路径 | 关网卡不等于能接现有的 vsock 代理 |
| 自己写的 Swift 助手 | 可以不添加 `VZVirtioNetworkDeviceConfiguration` | 可以 | 可以 | 要自己签名和维持 entitlement | 多一份要维护的二进制 |
| libkrun / krunvm 0.2.7 / krunkit | 没有 virtio-net 时，libkrun 仍会打开 TSI，套接字由宿主机代发 | 有，但是 TSI 的通道 | krunvm 的 `-v` | 已用于摘要 | 不能保证没有出网 |
| Lima `vz`、Tart | 能配成无网卡，但默认镜像和网络栈都比较重 | 有 | 有 | `brew` | 启动和镜像都比 vfkit 厚，不适合这个只读边界 |

vfkit 的用法说明：<https://github.com/crc-org/vfkit/blob/v0.6.4/doc/usage.md>。直接启动要用未压缩的 arm64 `Image`。内核钉在 cloud-hypervisor 的 `ch-release-v7.2.8-20261009` `Image-arm64`，和这台 Linux 上已经用过的 x86_64 内核是同一次发布。arm64 这份有没有 virtio-fs 和 vsock，要等 Mac 上的第一次启动。

## 在 M4 上跑

不要改 `.ssios`、`.ssios-demo` 或 `.ssios-memfs`。脚本用临时目录，失败时保留它。它不创建磁盘卷。

```bash
brew install vfkit
ollama --version
ollama pull qwen2.5-coder:7b
bash scripts/macos-vz-codex-smoke.sh
```

脚本把样例配置改成 `backend = "macos-vz"`、`agent = "codex"` 和本机 Ollama。`vm-prepare` 不接受 `--profile`，档位来自配置文件里的 `profile = "dev"`。`--profile dev` 只传给 `ssios up`。

Ollama 的 OpenAI 兼容接口 `/v1/responses` 忽略请求里的 `options`，上下文停在默认的 4096。Codex 的系统提示加工具大约 4098 token，所以那条路径会返回 400。宿主机代理把同一份请求译成原生 `POST /api/chat`，并带上 `[agent.codex] num_ctx`（默认 16384，允许 8192 到 32768）。已安装的 `qwen2.5-coder:7b` 不会被改写。冒烟脚本在启动客户机之前读 `/api/ps` 的 `context_length`。如果仍是 4096，脚本直接退出，并打印这两条补救命令：

```bash
OLLAMA_CONTEXT_LENGTH=16384 ollama serve
printf 'FROM qwen2.5-coder:7b\nPARAMETER num_ctx 16384\n' > /tmp/ssios-Modelfile
ollama create ssios-qwen2.5-coder-7b-16k -f /tmp/ssios-Modelfile
```

第二条只在重启后的 `/api/ps` 仍是 4096 时才需要。然后把 `[agent.codex] model` 改成 `ssios-qwen2.5-coder-7b-16k`，并把它加进 `allow_models`。上游 400 这类不可重试的 4xx 会立刻停掉 vfkit，请求上限仍是 4。

客户机根不会解到宿主机磁盘上。CPython 的 terminfo 里有 25 对只差大小写的路径，例如 `N/NCR260VT300WPP` 指向 `../n/ncr260vt300wpp`。在默认不区分大小写的 APFS 上这会变成指向自己的符号链接，Python 3.14 的 `tarfile` 过滤再调用 `realpath` 就会报 `Too many levels of symbolic links`。initramfs 在内存里从 tar 成员直接打成 cpio。`share/terminfo` 不放进客户机。每次冒烟都用新的临时目录，不必删上一次失败留下的目录。

vsock 的宿主机套接字不放在状态目录里。macOS 的 `sun_path` 只有 104 字节，`/var/folders/...` 太长。代理在 `/tmp/ssios-<随机>`（0700）上监听，套接字是 0600。vfkit 0.6.4 的 `listen` 表示宿主机听、客户机连到 CID 2 端口 19090。没有打开 vfkit 的 REST 套接字。串口日志仍写在任务目录的 `guest/serial.log`，失败时路径和末尾会进 `diagnostics/<task>.json`。Homebrew 的 vfkit 自带 `com.apple.security.virtualization` 签名，这里不再签名。

钉住的 `Image-arm64`（Linux 7.2.8）已经包含 virtio-vsock 和 virtio-fs。M4 上的串口日志有 `NET: Registered PF_VSOCK protocol family`。失败点是 python-build-standalone 的 CPython 3.12.16 musl 没有编译 `socket.AF_VSOCK`。中继用协议族 40，并通过 musl 的 `connect` 发送 16 字节的 `sockaddr_vm`。客户机在启动后几秒内自检；60 秒内没有 vsock 连接就停掉 vfkit。

客户机在 Codex 跑完之后，再用同一个 `workspace-write` 沙箱执行一次固定探测：写入 `/workspace/marker.txt`，并尝试写 `/opt/ssios-outside`。这一步不调用模型。冒烟只有在 `awaiting_approval`、`isolated` 为 true、`marker` 为 true、`sandbox_denied` 为 true、`outside_write` 为 false、`nic` 只有 `lo`、`outbound` 为 false 时才通过。缺标记是 `probe_marker_missing`，沙箱没有拒绝外部写入是 `probe_sandbox_not_denied`。
