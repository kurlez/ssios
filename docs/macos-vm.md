# macOS microVM 预检

这台开发机是 Linux，不能启动 macOS 的 Hypervisor.framework 客户机。下面的后端只生成检查结果和命令，不会把任务偷偷改回宿主机执行。

选择 `krunvm`（libkrun）。它在 Apple Silicon 上用 Hypervisor.framework 启动轻量 Linux 微虚拟机，比 vfkit 少一层 EFI 磁盘，也比仍在变化的 Apple `container` 命令更容易固定。vfkit 适合完整虚拟机，不适合每次任务都冷启动。本仓库不内置 krunvm 二进制。

```bash
brew tap slp/krun
brew install krunvm
krunvm --version
```

krunvm 自己的 README 目前写的是 `brew tap slp/krun`。Homebrew tap 仓库 README 也写过 `brew tap libkrun/krun` 和 `brew trust libkrun/krun`。这台 Linux 机器没有执行 brew，所以这里不写一个未核对的版本号。安装后以 `krunvm --version` 的输出为准。

配置里：

```toml
[runtime]
backend = "macos-vm"
```

然后：

```bash
sh scripts/macos-vm-smoke.sh
```

`isolated` 仍是 false，直到以后的版本真正在客户机里跑完任务并销毁虚拟机。客户机不会拿到人类令牌。Linux 上的 Cloud Hypervisor 路径仍然失败关闭；这台机器的 `/dev/kvm` 不能由当前用户打开。
