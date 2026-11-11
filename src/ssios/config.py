"""Configuration loading. Defaults stay on localhost and the mock provider."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    report_base_seconds: float
    report_batch_seconds: float
    report_per_100_seconds: float
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
    vfkit_bin: str
    macos_vm_volume: str
    guest_prepare_timeout_seconds: float
    runtime_agent: str
    codex_model: str
    codex_allow_models: tuple[str, ...]
    codex_max_requests: int
    codex_max_request_bytes: int
    codex_num_ctx: int
    task_timeout_seconds: float
    max_tool_calls: int
    max_output_bytes: int
    approval_ttl_seconds: float
    core_token_budget: int
    memory_mode: str
    letta_bin: str
    connectors_enabled: tuple[str, ...]
    connector_max_calls: int
    connector_max_bytes: int
    skills_enabled: bool
    skills_allow_domains: tuple[str, ...]
    skills_deny_domains: tuple[str, ...]
    skills_timeout_seconds: float
    skills_max_bytes: int
    skills_vault_dir: str
    ui_bind: str
    ui_time_zone: str
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
    if backend not in ("dev-guard", "cloud-hypervisor", "macos-vm", "macos-vz"):
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
    runtime_agent = str(runtime.get("agent", "summary"))
    if runtime_agent not in ("summary", "codex"):
        raise SsiosError("config_invalid", "runtime agent must be summary or codex", 500)
    codex = _section(data, "agent").get("codex", {})
    if codex is None:
        codex = {}
    if not isinstance(codex, dict):
        raise SsiosError("config_invalid", "agent.codex must be a table", 500)
    codex_model = str(codex.get("model", "qwen2.5-coder:7b"))
    if codex_model == "ollama" or any(char in codex_model for char in "\n\r\"/"):
        raise SsiosError("config_invalid", "codex model name is rejected", 500)
    raw_allow = codex.get("allow_models", [codex_model])
    if isinstance(raw_allow, str):
        raw_allow = [raw_allow]
    if not isinstance(raw_allow, list) or not raw_allow:
        raise SsiosError("config_invalid", "codex allow_models must be a list", 500)
    codex_allow_models = tuple(str(item) for item in raw_allow)
    if codex_model not in codex_allow_models:
        raise SsiosError("config_invalid", "codex model must be in allow_models", 500)
    if any(item == "ollama" or any(char in item for char in "\n\r\"/") for item in codex_allow_models):
        raise SsiosError("config_invalid", "codex model name is rejected", 500)
    codex_max_requests = int(codex.get("max_requests", 4))
    if not 1 <= codex_max_requests <= 8:
        raise SsiosError("config_invalid", "codex max_requests must be from 1 to 8", 500)
    codex_max_request_bytes = int(codex.get("max_request_bytes", 200000))
    if not 1024 <= codex_max_request_bytes <= 2000000:
        raise SsiosError("config_invalid", "codex max_request_bytes is out of range", 500)
    # Codex's first request is about 4098 tokens. Ollama's OpenAI-compatible
    # /v1/responses ignores per-request options and stays at 4096. The proxy
    # sends this value as options.num_ctx on native /api/chat.
    codex_num_ctx = int(codex.get("num_ctx", 16384))
    if not 8192 <= codex_num_ctx <= 32768:
        raise SsiosError("config_invalid", "agent.codex num_ctx must be from 8192 to 32768", 500)
    connectors = _section(data, "connectors")
    raw_enabled = connectors.get("enabled", [])
    if raw_enabled is None:
        raw_enabled = []
    if isinstance(raw_enabled, str):
        raw_enabled = [raw_enabled]
    if not isinstance(raw_enabled, list) or any(item not in ("drive", "gmail", "calendar") for item in raw_enabled):
        raise SsiosError("config_invalid", "connectors.enabled must list drive, gmail, and/or calendar", 500)
    connectors_enabled = tuple(str(item) for item in raw_enabled)
    connector_max_calls = int(connectors.get("max_calls", 40))
    connector_max_bytes = int(connectors.get("max_bytes", 200000))
    if not 1 <= connector_max_calls <= 100 or not 1 <= connector_max_bytes <= 2000000:
        raise SsiosError("config_invalid", "connector budget is out of range", 500)
    skills = _section(data, "skills")
    skills_enabled = bool(skills.get("enabled", False))
    skills_allow_domains = _host_list(skills.get("allow_domains", []))
    skills_deny_domains = _host_list(skills.get("deny_domains", []))
    skills_timeout_seconds = float(skills.get("timeout_seconds", 8))
    skills_max_bytes = int(skills.get("max_bytes", 200000))
    skills_vault_dir = str(skills.get("vault_dir", "") or "")
    if not 1 <= skills_timeout_seconds <= 30 or not 1024 <= skills_max_bytes <= 2000000:
        raise SsiosError("config_invalid", "skills budget is out of range", 500)
    ui = _section(data, "ui")
    ui_bind = str(ui.get("bind", "127.0.0.1"))
    if ui_bind == "localhost":
        ui_bind = "127.0.0.1"
    if ui_bind not in ("127.0.0.1", "0.0.0.0"):
        raise SsiosError("config_invalid", "ui bind must be 127.0.0.1 or 0.0.0.0", 500)
    ui_time_zone = str(ui.get("time_zone", "") or "")
    if ui_time_zone:
        try:
            ZoneInfo(ui_time_zone)
        except ZoneInfoNotFoundError as exc:
            raise SsiosError("config_invalid", "ui time_zone must be an IANA time zone name", 500) from exc
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
        report_base_seconds=float(llm.get("report_base_seconds", 45)),
        report_batch_seconds=float(llm.get("report_batch_seconds", 20)),
        report_per_100_seconds=float(llm.get("report_per_100_seconds", 8)),
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
        vfkit_bin=str(runtime.get("vfkit_bin", "vfkit")),
        macos_vm_volume=macos_vm_volume,
        guest_prepare_timeout_seconds=guest_prepare_timeout_seconds,
        runtime_agent=runtime_agent,
        codex_model=codex_model,
        codex_allow_models=codex_allow_models,
        codex_max_requests=codex_max_requests,
        codex_max_request_bytes=codex_max_request_bytes,
        codex_num_ctx=codex_num_ctx,
        task_timeout_seconds=float(limits.get("task_timeout_seconds", 30)),
        max_tool_calls=int(limits.get("max_tool_calls", 8)),
        max_output_bytes=int(limits.get("max_output_bytes", 65536)),
        approval_ttl_seconds=ttl,
        core_token_budget=budget,
        memory_mode=memory_mode,
        letta_bin=str(memory.get("letta_bin", "")),
        connectors_enabled=connectors_enabled,
        connector_max_calls=connector_max_calls,
        connector_max_bytes=connector_max_bytes,
        skills_enabled=skills_enabled,
        skills_allow_domains=skills_allow_domains,
        skills_deny_domains=skills_deny_domains,
        skills_timeout_seconds=skills_timeout_seconds,
        skills_max_bytes=skills_max_bytes,
        skills_vault_dir=skills_vault_dir,
        ui_bind=ui_bind,
        ui_time_zone=ui_time_zone,
        state_dir=state_path,
        config_path=config_path,
    )


def _host_list(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise SsiosError("config_invalid", "skill domains must be a list of host names", 500)
    hosts = []
    for item in value:
        host = str(item or "").strip().lower().rstrip(".")
        if host:
            hosts.append(host)
    return tuple(hosts)


def lan_enabled(settings) -> bool:
    return settings.ui_bind == "0.0.0.0"


def connector_config_location(settings) -> str:
    """Where an administrator edits [connectors] enabled. Includes the line when it is present."""
    path = getattr(settings, "config_path", None)
    if path is None:
        return "the SSIOS config, under [connectors] enabled"
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return f"{path}, under [connectors] enabled"
    section = ""
    found = 0
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue
        if section == "connectors" and not stripped.startswith("#") and stripped.startswith("enabled"):
            found = index
            break
    if found:
        return f"{path} line {found}"
    return f"{path}, under [connectors] enabled"
