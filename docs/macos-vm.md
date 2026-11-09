# macOS microVM

Apple Silicon 上的客户机用 `krunvm`（libkrun，Hypervisor.framework）。Linux 上的同一条命令在 KVM 可用时也能用。这台开发机是 Linux，不能启动 Hypervisor.framework。Cloud Hypervisor 是 Linux 上的第二条路径。

客户机里跑的是 `guest_worker.py`。它只读本次任务复制出来的 `grant.json`，把提示写到 scratch 的 `request.json`，等宿主机把模型回答写到 `response.json`，再把正文写到 `output.json`。客户机没有人类令牌，也没有模型地址。模型调用发生在宿主机上，仍然只走配置里的 mock 或本机 Ollama。质量检查和人类批准仍在宿主机上。`isolated` 只有在客户机进程确实退出且写出了 `output.json` 时才是 true。

`krunvm create -v` 没有只读参数（手册 `krunvm-create` 只有 `HOST_PATH:GUEST_PATH`）。因此不会把记忆仓库直接挂进去，而是把授权正文复制到本次任务自己的目录再挂载。客户机改这份副本不会写回记忆。本机这套 `virtiofsd` 同样没有只读开关，Cloud Hypervisor 也是挂复制出来的目录。

krunvm 没有关闭客户机网络的参数，所以不能从命令行证明 TSI 已关闭。worker 自己不打开套接字。Cloud Hypervisor 的启动参数里没有 `--net`。

## 安装

本仓库不附带 krunvm。krunvm 自己的 README 目前写的是：

```bash
brew tap slp/krun
brew install krunvm
brew install buildah
krunvm --version
```

Homebrew tap 仓库也写过 `brew tap libkrun/krun` 和 `brew trust libkrun/krun`。哪条能装上，以你机器上的 `brew` 为准。装完执行 `krunvm --version`，版本以那次输出为准。这里不写一个没有在 Mac 上执行过的瓶装哈希。

`krunvm create` 通过 buildah 拉取 OCI 镜像。镜像钉在 `python:3.12-alpine` 的清单摘要上（2026-11-09 从 `registry-1.docker.io` 读到）：

```text
docker.io/library/python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7
```

macOS 的默认 APFS 不区分大小写。buildah 的 overlay 存储经常因此失败。需要的话，由你自己建一个区分大小写的卷，再把 containers 存储指过去。本仓库的脚本不会执行 `hdiutil`，也不会改磁盘分区：

```bash
hdiutil create -size 20g -fs "Case-sensitive APFS" -type SPARSE -volname ssios-containers "$HOME/ssios-containers.dmg"
hdiutil attach "$HOME/ssios-containers.dmg"
mkdir -p /Volumes/ssios-containers/storage /Volumes/ssios-containers/run "$HOME/.config/containers"
cat > "$HOME/.config/containers/storage.conf" <<'EOF'
[storage]
driver = "overlay"
graphroot = "/Volumes/ssios-containers/storage"
runroot = "/Volumes/ssios-containers/run"
EOF
```

## 配置

在你自己的配置里（不要改被跟踪的样例，除非你愿意）把后端改成：

```toml
[runtime]
backend = "macos-vm"
```

然后用一份临时状态做冒烟。它不会动 `.ssios`、`.ssios-demo` 或 `.ssios-memfs`：

```bash
bash scripts/macos-vm-smoke.sh
PYTHONPATH=src python3 -m ssios vm-doctor --state /tmp/ssios-vm-doctor --config /tmp/ssios-vm-doctor-does-not-exist
```

`vm-doctor` 要读一份 `backend = "macos-vm"` 的配置。它会创建一台客户机，在里面执行 `echo ok`，打印耗时，然后删除这台虚拟机。预检本身不启动客户机。

确认 `krunvm` 可用之后，再把正在跑的服务停掉，用原来的状态目录和新的 `backend` 启动。启动失败时任务失败关闭，不会改在 Mac 宿主机上跑摘要。

这台 Linux 机器没有 krunvm。在把 `/dev/kvm` 交给当前用户之后，Cloud Hypervisor v54.0 配 Linux 7.2.8 内核实测：`echo ok` 898 毫秒；摘要 worker（virtio-fs，无 `--net`，模型在宿主机 mock）1576 毫秒，`isolated` 为 true。这不是 Mac 上的数字。
