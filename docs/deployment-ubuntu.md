# Ubuntu and Cloud Hypervisor

The household UI on a desktop, including Linux, is `./scripts/ssios start`. See the README. This page is only the Cloud Hypervisor preflight. That preflight does not boot a guest.

The production target is KVM on Ubuntu LTS, plus Cloud Hypervisor and virtiofsd. The dependencies below are what the operator prepares. The repository does not ship an install script that turns security options off, and it does not use a Docker socket or a shared user home directory.

## Machine prerequisites

- Ubuntu 24.04 LTS or a newer LTS. The kernel needs virtio-fs. Upstream Cloud Hypervisor docs ask for about 5.10 or newer
- `/dev/kvm` exists, and the user running SSIOS is in the `kvm` group and can read and write that device
- The control plane is not run as root
- The architecture can be x86_64 or aarch64, but the guest kernel, firmware, and disk image must match the host architecture. ARM64 can be used. An amd64 image cannot be booted directly on it
- Whether a Linux guest on Apple Silicon exposes nested KVM is outside what this guide guarantees

This prototype's preflight checks:

- `kvm_path`, `/dev/kvm` by default
- whether `cloud-hypervisor` is executable
- whether `virtiofsd` is executable
- whether `kernel_path` points at a regular file rather than a symlink
- whether `root_image_path` points at the task guest's root disk
- the SSIOS agent protocol inside the guest. This item is a fixed failure in v0

If any item fails, `ok` is false. Even when every external condition is met, `execution_implemented` is still false and the service does not boot a virtual machine.

## Images that are required

The operator prepares these. They are not downloaded from this repository:

- A guest kernel that supports PVH or matches the chosen firmware
- A root disk that contains only the task runtime. Do not bake the user home directory or the memory repository into it
- A private writable disk for each task. Do not mount the same ext4 image writable on two virtual machines
- Two virtiofsd processes, one for the input export and one for output staging. Add `--readonly` to the input. Both use `--sandbox namespace` and `--cache never`

The Cloud Hypervisor side needs `--memory size=<MB>,shared=on` before virtio-fs can be used. Do not add `--net` to the command unless a separately implemented and reviewed network broker exists later. The v0 builder rejects `network=true` outright.

The memory repository, `vault/`, `secrets/`, and `auth/` cannot be a `--shared-dir`. The host hands an already granted export directory to virtiofsd only after the task token authenticates. In v0 that handoff is only a path plan. There is no actual daemon.

## Suggested packages

Install these from the distribution on the target machine, and check the versions yourself:

- `cloud-hypervisor`
- `virtiofsd`, preferring the Rust virtiofsd
- Build or obtain a guest kernel that matches the architecture

After installation, look at the preflight with the command below. Do not expect it to boot a virtual machine:

```bash
PYTHONPATH=src python3 -m ssios preflight --profile production --state .ssios-preflight --config config/ssios.example.toml
```

Exit code 0 only means the command printed JSON. `ok` inside that JSON should be false in this prototype.

## A clean pool and snapshots

A single-use clean warm pool is an architecture requirement, but it is not implemented. The snapshot interface returns `snapshot_forbidden` for a vault, memory, tokens, and the user home directory. For other task directories it returns `snapshot_not_implemented` and copies no files.

Do not snapshot the memory of a virtual machine that has held memory or tokens and then clone it. If restore is implemented later, the guest identity and injected keys must be reset first. That reset is not in the current code.

## Disk encryption

Enable LUKS in the Ubuntu installer, then put the SSIOS state directory on the unlocked filesystem. The application itself does not create a LUKS volume.
