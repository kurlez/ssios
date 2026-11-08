# Ubuntu 与 Cloud Hypervisor

生产目标是 Ubuntu LTS 上的 KVM，加上 Cloud Hypervisor 和 virtiofsd。下面是操作者需要自行准备的依赖。仓库不附带关闭安全选项的安装脚本，也不使用 Docker socket 或共享的用户主目录。

## 机器前提

- Ubuntu 24.04 LTS 或更新的 LTS。内核需要带 virtio-fs，上游 Cloud Hypervisor 文档要求大约 5.10 以上
- `/dev/kvm` 存在，并且运行 SSIOS 的用户在 `kvm` 组里，能够读写该设备
- 不使用 root 跑控制面
- 架构可以是 x86_64 或 aarch64，但客户机内核、固件和磁盘镜像必须与宿主机架构一致。ARM64 可用，不能拿 amd64 镜像直接启动
- Apple Silicon 上的 Linux 客户机是否暴露嵌套 KVM，不在本指南的保证范围内

本原型的预检会检查：

- `kvm_path`，默认 `/dev/kvm`
- `cloud-hypervisor` 是否可执行
- `virtiofsd` 是否可执行
- `kernel_path` 指向一个普通文件而不是符号链接
- `root_image_path` 指向任务客户机的根磁盘
- 客户机里的 SSIOS 代理协议。这一项在 v0 固定失败

任何一项失败，`ok` 都是 false。全部外在条件满足时，`execution_implemented` 仍是 false，服务不会启动虚拟机。

## 需要的镜像

操作者自己准备，不从本仓库下载：

- 一个支持 PVH 或与所选固件匹配的客户机内核
- 一个只含任务运行时的根磁盘，不要把用户主目录或记忆库烤进去
- 每个任务一块私有的可写磁盘。不要把同一 ext4 镜像以可写方式挂给两个虚拟机
- 输入导出和输出暂存用两个 virtiofsd。输入加 `--readonly`。两者都使用 `--sandbox namespace` 和 `--cache never`

Cloud Hypervisor 侧需要 `--memory size=<MB>,shared=on` 才能使用 virtio-fs。命令里不要加 `--net`，除非将来有单独实现并经过审查的网络经纪。v0 的构造器在 `network=true` 时直接拒绝。

记忆仓库、`vault/`、`secrets/` 和 `auth/` 不能作为 `--shared-dir`。宿主机在任务令牌通过认证之后，才把已经授权的导出目录交给 virtiofsd。这个释放步骤在 v0 只有路径计划，没有实际的守护进程。

## 建议的包

在目标机器上用发行版来源安装，并自己核对版本：

- `cloud-hypervisor`
- `virtiofsd`，优先使用 Rust 实现的 virtiofsd
- 构建或取得与架构匹配的客户机内核

安装后用下面的命令看预检，而不是期待它启动虚拟机：

```bash
PYTHONPATH=src python3 -m ssios preflight --profile production --state .ssios-preflight --config config/ssios.example.toml
```

退出码 0 只表示命令打印了 JSON。JSON 里的 `ok` 在本原型中应为 false。

## 干净池和快照

单次使用的干净预热池是架构要求，但还没有实现。快照接口对 vault、记忆、令牌和用户主目录返回 `snapshot_forbidden`。对其余任务目录返回 `snapshot_not_implemented`，并且不复制文件。

不要把装过记忆或令牌的虚拟机内存做成快照再克隆。将来如果实现恢复，必须先重置客户机身份和注入的密钥。该重置没有写在当前代码里。

## 磁盘加密

在 Ubuntu 安装器里启用 LUKS，再把 SSIOS 状态目录放在解密后的文件系统上。应用本身不会创建 LUKS 卷。
