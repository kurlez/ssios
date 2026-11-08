"""Command line entry points."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from ssios.client import request
from ssios.config import load_settings
from ssios.httpapi import ControlServer
from ssios.runtime import CloudHypervisorRuntime
from ssios.service import ControlPlane, production_blocked
from ssios.upstream import describe_memfs, probe_local_binary


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default=".ssios", help="state directory")
    common.add_argument("--config", default="config/ssios.example.toml", help="config file")
    parser = argparse.ArgumentParser(prog="ssios", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "up", "demo", "health", "status", "preflight", "memfs-info"):
        command = sub.add_parser(name, parents=[common])
        if name in ("up", "demo", "preflight"):
            command.add_argument("--profile", choices=("dev", "production"))
            command.add_argument("--port", type=int)
        if name == "memfs-info":
            command.add_argument("--execute-version", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "memfs-info":
        print(json.dumps({"memfs": describe_memfs(), "probe": probe_local_binary(execute=args.execute_version)}, indent=2, ensure_ascii=False))
        return 0
    settings = load_settings(
        Path(args.config),
        state_dir=Path(args.state),
        profile=getattr(args, "profile", None),
        port=getattr(args, "port", None),
    )
    if args.command == "init":
        ControlPlane(settings).open()
        print(f"状态目录已就绪：{settings.state_dir}")
        print("令牌写在 secrets/human.token 与 secrets/worker.token，权限为 0600。")
        return 0
    if args.command == "preflight":
        print(json.dumps(CloudHypervisorRuntime(settings).preflight(), indent=2, ensure_ascii=False))
        return 0
    if args.command == "up":
        return _up(settings)
    if args.command in ("health", "status"):
        return _get(settings, "/v1/health" if args.command == "health" else "/v1/status")
    if args.command == "demo":
        return run_demo(settings)
    return 2


def _up(settings) -> int:
    if production_blocked(settings):
        report = CloudHypervisorRuntime(settings).preflight()
        print(json.dumps({"error": "microvm_fail_closed", "preflight": report}, indent=2, ensure_ascii=False))
        return 2
    plane = ControlPlane(settings)
    plane.open()
    server = ControlServer(plane)
    server.start()
    print(f"SSIOS 开发配置已在 http://{settings.host}:{plane.bound_port} 监听")
    print(f"配置档：{settings.profile}，模型：{settings.llm_provider}，内存模式：letta-memfs-compat")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
        return 0


def _get(settings, path: str) -> int:
    plane = ControlPlane(settings)
    plane.open()
    token = plane.auth.read_secret("human")
    status, payload, _headers = request(f"http://{settings.host}:{settings.port}", "GET", path, token=token)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if status == 200 else 1


def run_demo(settings) -> int:
    plane = ControlPlane(settings)
    plane.open()
    server = ControlServer(plane)
    server.start()
    base = f"http://127.0.0.1:{plane.bound_port}"
    human = plane.auth.read_secret("human")
    try:
        status, health, _headers = request(base, "GET", "/v1/health", token=human)
        if status != 200 or health.get("llm_provider") != "mock":
            raise RuntimeError("health check failed")
        status, created, _headers = request(
            base,
            "POST",
            "/v1/tasks",
            token=human,
            body={
                "project_id": "demo-alpha",
                "kind": "project-summary",
                "input_revision": "exp-1",
                "input_path": "notes.md",
            },
        )
        if status != 202:
            raise RuntimeError(f"task create failed: {created}")
        task = _poll(base, human, created["id"], {"awaiting_approval", "failed", "timeout", "cancelled"})
        if task["status"] != "awaiting_approval":
            raise RuntimeError(f"task did not reach approval: {task}")
        for approval_id in task["approval_ids"]:
            code, approval, _headers = request(base, "GET", f"/v1/approvals/{approval_id}", token=human)
            if code != 200:
                raise RuntimeError(f"preview failed: {approval}")
            decision, result, _headers = request(
                base,
                "POST",
                f"/v1/approvals/{approval_id}/decide",
                token=human,
                body={
                    "decision": "approve",
                    "action": approval["action"],
                    "payload_sha256": approval["payload_sha256"],
                    "expected_revision": approval["expected_revision"],
                    "bound_hash": approval["bound_hash"],
                },
            )
            if decision != 200:
                raise RuntimeError(f"approval failed: {result}")
        task = _poll(base, human, created["id"], {"committed", "failed", "rejected"})
        if task["status"] != "committed":
            raise RuntimeError(f"task did not commit: {task}")
        server.stop()
        restarted = ControlPlane(settings)
        restarted.open()
        server = ControlServer(restarted)
        server.start()
        base = f"http://127.0.0.1:{restarted.bound_port}"
        human = restarted.auth.read_secret("human")
        code, core, _headers = request(base, "GET", "/v1/memory/core?project_id=demo-alpha", token=human)
        code2, topic, _headers = request(
            base,
            "GET",
            "/v1/memory/file?project_id=demo-alpha&path=topics/fixtures/last-summary.md",
            token=human,
        )
        code3, published, _headers = request(
            base,
            "GET",
            "/v1/storage/published?project_id=demo-alpha&revision=HEAD",
            token=human,
        )
        if code != 200 or code2 != 200 or code3 != 200:
            raise RuntimeError("restart read failed")
        core_blob = json.dumps(core, ensure_ascii=False)
        if "CORE-PROJECT-FACT" not in core_blob or "NORTHWIND-FIXTURE-NOTE" in core_blob:
            raise RuntimeError("core memory scope is wrong after restart")
        if topic.get("trust") != "approved" or "NORTHWIND-FIXTURE-NOTE" not in topic.get("content", ""):
            raise RuntimeError("approved topic memory missing after restart")
        if "THREE-BULLET-RULE" not in published.get("content", ""):
            raise RuntimeError("published summary missing")
        audit_text = (settings.state_dir / "audit" / "audit.jsonl").read_text(encoding="utf-8")
        if human.strip() in audit_text or "NORTHWIND-FIXTURE-NOTE" in audit_text:
            raise RuntimeError("audit log leaked a token or raw fixture text")
        print("演示完成。")
        print(f"任务 {task['id']} 已由人类控制令牌批准并提交。")
        print(f"发布修订：{task['published_revision']}")
        print(f"记忆修订：{task['memory_revision']}")
        print("重启后核心记忆仍可加载，主题摘要按路径读取，审计日志只保留哈希。")
        return 0
    finally:
        server.stop()


def _poll(base: str, token: str, task_id: str, terminal: set[str], timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        status, last, _headers = request(base, "GET", f"/v1/tasks/{task_id}", token=token)
        if status == 200 and last.get("status") in terminal:
            return last
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {terminal}: {last}")


if __name__ == "__main__":
    raise SystemExit(main())
