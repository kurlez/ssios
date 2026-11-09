"""Configuration loading. Defaults stay on localhost and the mock provider."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ssios.errors import SsiosError


@dataclass(frozen=True)
class Settings:
    profile: str
    host: str
    port: int
    llm_provider: str
    ollama_base_url: str
    ollama_model: str
    llm_timeout_seconds: float
    max_output_tokens: int
    num_ctx: int
    summary_repair_retries: int
    max_note_chars: int
    runtime_backend: str
    cpus: int
    memory_mb: int
    kvm_path: str
    cloud_hypervisor_bin: str
    virtiofsd_bin: str
    kernel_path: str
    root_image_path: str
    initramfs_path: str
    echo_initramfs_path: str
    guest_image: str
    krunvm_bin: str
    macos_vm_volume: str
    guest_prepare_timeout_seconds: float
    task_timeout_seconds: float
    max_tool_calls: int
    max_output_bytes: int
    approval_ttl_seconds: float
    core_token_budget: int
    memory_mode: str
    letta_bin: str
    state_dir: Path
    config_path: Path | None


def _section(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise SsiosError("config_invalid", f"{name} must be a table", 500)
    return value


def load_settings(
    config_path: Path | None,
    *,
    state_dir: Path,
    profile: str | None = None,
    port: int | None = None,
) -> Settings:
    data: dict = {}
    if config_path is not None:
        config_path = Path(config_path).expanduser()
        if not config_path.is_file():
            raise SsiosError("config_missing", "config file not found", 500)
        config_path = config_path.resolve()
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    server = _section(data, "server")
    llm = _section(data, "llm")
    runtime = _section(data, "runtime")
    limits = _section(data, "limits")
    memory = _section(data, "memory")
    chosen_profile = profile or str(data.get("profile", "dev"))
    if chosen_profile not in ("dev", "production"):
        raise SsiosError("config_invalid", "profile must be dev or production", 500)
    host = str(server.get("host", "127.0.0.1"))
    if host not in ("127.0.0.1", "localhost"):
        raise SsiosError("config_invalid", "server host must stay on loopback", 500)
    chosen_port = int(port if port is not None else server.get("port", 8765))
    if not 0 <= chosen_port <= 65535:
        raise SsiosError("config_invalid", "port out of range", 500)
    provider = str(llm.get("provider", "mock"))
    if provider not in ("mock", "ollama"):
        raise SsiosError("config_invalid", "llm provider must be mock or ollama", 500)
    backend = str(runtime.get("backend", "dev-guard"))
    if backend not in ("dev-guard", "cloud-hypervisor", "macos-vm"):
        raise SsiosError("config_invalid", "runtime backend is not recognized", 500)
    if chosen_profile == "production":
        backend = "cloud-hypervisor"
    ollama_base = str(llm.get("ollama_base_url", "http://127.0.0.1:11434"))
    if not (ollama_base.startswith("http://127.0.0.1:") or ollama_base.startswith("http://localhost:")):
        raise SsiosError("config_invalid", "ollama base url must target loopback", 500)
    memory_mode = str(memory.get("mode", "letta-memfs-compat"))
    if memory_mode not in ("letta-memfs-compat", "letta-memfs-upstream"):
        raise SsiosError("config_invalid", "memory mode must be letta-memfs-compat or letta-memfs-upstream", 500)
    budget = int(limits.get("core_token_budget", 2000))
    if budget < 16:
        raise SsiosError("config_invalid", "core token budget is too small", 500)
    ttl = float(limits.get("approval_ttl_seconds", 600))
    if ttl < 0:
        raise SsiosError("config_invalid", "approval ttl must be >= 0", 500)
    max_output_tokens = int(llm.get("max_output_tokens", 1024))
    if not 64 <= max_output_tokens <= 8192:
        raise SsiosError("config_invalid", "max_output_tokens must be from 64 to 8192", 500)
    num_ctx = int(llm.get("num_ctx", 8192))
    if not 2048 <= num_ctx <= 32768:
        raise SsiosError("config_invalid", "num_ctx must be from 2048 to 32768", 500)
    summary_repair_retries = int(llm.get("summary_repair_retries", 1))
    if summary_repair_retries not in (0, 1):
        raise SsiosError("config_invalid", "summary_repair_retries must be 0 or 1", 500)
    max_note_chars = int(llm.get("max_note_chars", 6000))
    if not 500 <= max_note_chars <= 20000:
        raise SsiosError("config_invalid", "max_note_chars must be from 500 to 20000", 500)
    macos_vm_volume = str(runtime.get("macos_vm_volume", "/Volumes/krunvm"))
    if not macos_vm_volume.startswith("/") or ".." in Path(macos_vm_volume).parts:
        raise SsiosError("config_invalid", "macos_vm_volume must be an absolute path", 500)
    guest_prepare_timeout_seconds = float(runtime.get("guest_prepare_timeout_seconds", 600))
    if guest_prepare_timeout_seconds < 30:
        raise SsiosError("config_invalid", "guest_prepare_timeout_seconds must be at least 30", 500)
    state_path = Path(state_dir).expanduser()
    if not state_path.is_absolute():
        state_path = Path.cwd() / state_path
    state_path = state_path.resolve()
    return Settings(
        profile=chosen_profile,
        host=host,
        port=chosen_port,
        llm_provider=provider,
        ollama_base_url=ollama_base.rstrip("/"),
        ollama_model=str(llm.get("ollama_model", "llama3.1:8b")),
        llm_timeout_seconds=float(llm.get("request_timeout_seconds", 60)),
        max_output_tokens=max_output_tokens,
        num_ctx=num_ctx,
        summary_repair_retries=summary_repair_retries,
        max_note_chars=max_note_chars,
        runtime_backend=backend,
        cpus=int(runtime.get("cpus", 1)),
        memory_mb=int(runtime.get("memory_mb", 512)),
        kvm_path=str(runtime.get("kvm_path", "/dev/kvm")),
        cloud_hypervisor_bin=str(runtime.get("cloud_hypervisor_bin", "cloud-hypervisor")),
        virtiofsd_bin=str(runtime.get("virtiofsd_bin", "virtiofsd")),
        kernel_path=str(runtime.get("kernel_path", "")),
        root_image_path=str(runtime.get("root_image_path", "")),
        initramfs_path=str(runtime.get("initramfs_path", "")),
        echo_initramfs_path=str(runtime.get("echo_initramfs_path", "")),
        guest_image=str(
            runtime.get(
                "guest_image",
                "docker.io/library/python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7",
            )
        ),
        krunvm_bin=str(runtime.get("krunvm_bin", "krunvm")),
        macos_vm_volume=macos_vm_volume,
        guest_prepare_timeout_seconds=guest_prepare_timeout_seconds,
        task_timeout_seconds=float(limits.get("task_timeout_seconds", 30)),
        max_tool_calls=int(limits.get("max_tool_calls", 8)),
        max_output_bytes=int(limits.get("max_output_bytes", 65536)),
        approval_ttl_seconds=ttl,
        core_token_budget=budget,
        memory_mode=memory_mode,
        letta_bin=str(memory.get("letta_bin", "")),
        state_dir=state_path,
        config_path=config_path,
    )
