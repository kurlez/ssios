"""Command line entry points."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from dataclasses import replace

from ssios.client import request
from ssios.config import load_settings
from ssios.errors import SsiosError
from ssios.gc import collect as collect_leftovers
from ssios.httpapi import ControlServer
from ssios.ingest import run_ingest
from ssios.letta_upstream import UpstreamLettaBridge
from ssios.llm import probe_ollama
from ssios.runtime import CloudHypervisorRuntime
from ssios.service import ControlPlane, production_blocked
from ssios.summary import run_summary
from ssios.upstream import describe_memfs, probe_local_binary


def build_parser() -> argparse.ArgumentParser:
    """The real command parser. `--profile` is only on commands that start or talk as a profile."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default=".ssios", help="state directory")
    common.add_argument("--config", default="config/ssios.example.toml", help="config file")
    parser = argparse.ArgumentParser(prog="ssios", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "up", "ui", "demo", "health", "status", "preflight", "summary", "ingest", "diagnose", "gc", "vm-doctor", "vm-diagnose", "vm-prepare", "model-bench", "router-bench", "library-embed-bench", "ui-login", "ollama-probe", "memfs-info", "memfs-upstream-smoke", "user", "audit-verify"):
        command = sub.add_parser(name, parents=[common])
        if name in ("up", "ui", "demo", "preflight", "summary"):
            command.add_argument("--profile", choices=("dev", "production"))
            command.add_argument("--port", type=int)
        if name == "summary":
            command.add_argument("--project", default="local-summary")
            command.add_argument("--topic", default="")
        if name == "ingest":
            command.add_argument("source")
            command.add_argument("--project", required=True)
        if name == "diagnose":
            command.add_argument("task_id")
        if name == "gc":
            command.add_argument("--dry-run", action="store_true")
        if name == "model-bench":
            command.add_argument("--model", default="", help="Ollama model name, such as qwen3:14b or muse-glimmer:30b-mlx")
            command.add_argument("--vision", action="store_true", help="also score one synthetic image caption")
            command.add_argument("--vision-model", default="", help="vision model, default qwen2.5vl:7b")
        if name == "router-bench":
            command.add_argument("--rules-only", action="store_true", help="score the rules and do not call Ollama")
            command.add_argument("--model", action="append", default=[], help="classifier model, repeatable. Default qwen3:1.7b and qwen3:4b")
        if name == "ui":
            command.add_argument("--lan", action="store_true")
            command.add_argument("--check", action="store_true", help="print chat guest prerequisites and exit")
        if name == "user":
            command.add_argument("user_action", choices=("add",))
            command.add_argument("--admin", action="store_true")
            command.add_argument("--username", required=True)
        if name == "ui-login":
            pass
        if name == "memfs-info":
            command.add_argument("--execute-version", action="store_true")
        if name == "memfs-upstream-smoke":
            command.add_argument("--letta-bin", default="")
    for name in ("oauth-install-client", "oauth-connect", "oauth-status", "oauth-disconnect", "oauth-revoke-account", "connector-fetch"):
        command = sub.add_parser(name, parents=[common])
        command.add_argument("--account", required=True)
        if name in ("oauth-connect", "oauth-disconnect"):
            command.add_argument("--connector", required=True, choices=("drive", "gmail", "calendar"))
        if name == "oauth-connect":
            command.add_argument("--scope", default="")
            command.add_argument("--no-browser", action="store_true")
        if name == "connector-fetch":
            command.add_argument("--project", required=True)
            command.add_argument("--tool", required=True)
            command.add_argument("--arguments", default="{}")
            command.add_argument("--limits", default="{}")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except (OSError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if args.command == "memfs-info":
        print(json.dumps({"memfs": describe_memfs(), "probe": probe_local_binary(execute=args.execute_version)}, indent=2, ensure_ascii=False))
        return 0
    if args.command == "memfs-upstream-smoke":
        return run_upstream_smoke(Path(args.state), args.config, args.letta_bin)
    if args.command in ("oauth-install-client", "oauth-connect", "oauth-status", "oauth-disconnect", "oauth-revoke-account", "connector-fetch"):
        try:
            settings = load_settings(Path(args.config), state_dir=Path(args.state), port=0)
        except SsiosError as exc:
            print(json.dumps({"error": exc.code, "detail": exc.detail}, ensure_ascii=False))
            return 1
        return run_google_command(settings, args)
    try:
        settings = load_settings(
            Path(args.config),
            state_dir=Path(args.state),
            profile=getattr(args, "profile", None),
            port=getattr(args, "port", None),
        )
    except SsiosError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail}, ensure_ascii=False))
        return 1
    if args.command == "init":
        ControlPlane(settings).open()
        print(f"State directory is ready: {settings.state_dir}")
        print("Tokens are in secrets/human.token and secrets/worker.token, mode 0600.")
        return 0
    if args.command == "preflight":
        print(json.dumps(CloudHypervisorRuntime(settings).preflight(), indent=2, ensure_ascii=False))
        return 0
    if args.command == "up":
        return _up(settings)
    if args.command == "ui":
        if getattr(args, "lan", False):
            settings = replace(settings, ui_bind="0.0.0.0")
        if getattr(args, "check", False):
            from ssios.chat_guest import chat_guest_report
            from ssios.secret_store import probe_keychain

            report = chat_guest_report(settings)
            report["keychain"] = probe_keychain()
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            ready = bool(report.get("ok")) and bool(report["keychain"].get("ok"))
            return 0 if ready else 1
        return _up(settings)
    if args.command == "user":
        return run_user_add(settings, args.username, admin=bool(args.admin))
    if args.command == "audit-verify":
        plane = ControlPlane(settings)
        plane.open()
        report = plane.security_audit.verify()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1
    if args.command in ("health", "status"):
        return _get(settings, "/v1/health" if args.command == "health" else "/v1/status")
    if args.command == "demo":
        if getattr(args, "port", None) is None:
            settings = replace(settings, port=0)
        return run_demo(settings)
    if args.command == "ollama-probe":
        return run_ollama_probe(settings)
    if args.command == "ui-login":
        from ssios.ui import issue_login_code

        plane = ControlPlane(settings)
        plane.open()
        code = issue_login_code(plane)
        print(f"http://127.0.0.1:{settings.port}/ui/login?code={code}")
        print("This code is valid for about two minutes and is stored only as a hash in the local state directory.")
        return 0
    if args.command == "vm-prepare":
        return run_vm_prepare(settings)
    if args.command == "vm-doctor":
        from ssios.runtime import build_runtime

        print(json.dumps(build_runtime(settings).doctor(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "vm-diagnose":
        from ssios.chat_guest import vm_diagnose

        report = vm_diagnose(settings)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0 if report.get("prerequisites", {}).get("ok") else 1
    if args.command == "router-bench":
        from ssios.router_bench import CLASSIFIER_MODELS, score_model, score_rules

        rules = score_rules()
        if args.rules_only:
            print(json.dumps(rules, ensure_ascii=False, indent=2), flush=True)
            return 0 if rules.get("accuracy") == 1 else 1
        models = list(args.model) or list(CLASSIFIER_MODELS)
        scored = []
        for model in models:
            try:
                scored.append(score_model(model, base_url=settings.ollama_base_url, timeout=settings.router_timeout_seconds))
            except (SsiosError, OSError, TimeoutError) as exc:
                detail = exc.detail if isinstance(exc, SsiosError) else exc.__class__.__name__
                scored.append({"mode": model, "error": detail})
        print(json.dumps({"rules": rules, "models": scored}, ensure_ascii=False, indent=2), flush=True)
        return 0 if rules.get("accuracy") == 1 else 1
    if args.command == "library-embed-bench":
        from ssios.library_embed import embed_bench

        print(json.dumps(embed_bench(settings.ollama_base_url), ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.command == "model-bench":
        from ssios.model_bench import run_bench

        model = str(getattr(args, "model", "") or "") or settings.codex_model
        try:
            report = run_bench(
                model=model,
                base_url=settings.ollama_base_url,
                num_ctx=int(settings.codex_num_ctx),
                vision=bool(getattr(args, "vision", False)),
                vision_model=str(getattr(args, "vision_model", "") or "") or settings.vision_model,
            )
        except SsiosError as exc:
            print(json.dumps({"error": exc.code, "detail": exc.detail}, ensure_ascii=False), flush=True)
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.command == "gc":
        return run_gc(settings, dry_run=args.dry_run)
    if args.command == "diagnose":
        return run_diagnose(settings, args.task_id)
    if args.command == "ingest":
        try:
            return run_ingest(settings, Path(args.source), args.project)
        except SsiosError as exc:
            print(json.dumps({"error": exc.code, "detail": exc.detail}, ensure_ascii=False), flush=True)
            return 1
        except KeyboardInterrupt:
            print(json.dumps({"error": "interrupted", "detail": "Interrupted. Memory was not written."}, ensure_ascii=False), flush=True)
            return 130
        except Exception as exc:
            print(json.dumps({"error": "internal", "detail": exc.__class__.__name__}, ensure_ascii=False), flush=True)
            return 1
    if args.command == "summary":
        try:
            return run_summary(settings, project_id=args.project, topic=args.topic)
        except SsiosError as exc:
            print(json.dumps({"error": exc.code, "detail": exc.detail}, ensure_ascii=False), flush=True)
            return 1
        except KeyboardInterrupt:
            print(json.dumps({"error": "interrupted", "detail": "Interrupted. Nothing was approved."}, ensure_ascii=False), flush=True)
            return 130
        except Exception as exc:
            print(json.dumps({"error": "internal", "detail": exc.__class__.__name__}, ensure_ascii=False), flush=True)
            return 1
    return 2


def run_user_add(settings, username: str, *, admin: bool) -> int:
    from ssios.accounts import public_account

    plane = ControlPlane(settings)
    plane.open()
    password = _read_password()
    if not password:
        print(json.dumps({"error": "password_rejected", "detail": "password was empty or the two entries did not match"}, ensure_ascii=False))
        return 1
    try:
        account = plane.accounts.add_admin(username, password) if admin else plane.accounts.add_member(username, password)
    except SsiosError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail}, ensure_ascii=False))
        return 1
    print(json.dumps(public_account(account), ensure_ascii=False, indent=2))
    return 0


def _read_password() -> str:
    if sys.stdin.isatty():
        import getpass

        first = getpass.getpass("Password: ")
        second = getpass.getpass("Repeat password: ")
        if first != second:
            return ""
        return first
    return sys.stdin.readline().rstrip("\n")


def run_vm_prepare(settings) -> int:
    from ssios.codex_guest import prepare_codex_binary
    from ssios.runtime import build_runtime
    from ssios.vmguest import prepare_guest_image

    reports = []
    if settings.runtime_agent == "codex":
        try:
            reports.append(prepare_codex_binary(settings.state_dir, timeout=settings.guest_prepare_timeout_seconds))
        except SsiosError as exc:
            print(json.dumps({"ok": False, "detail": exc.detail}, ensure_ascii=False, indent=2))
            return 1
    runtime = build_runtime(settings)
    if settings.runtime_backend == "macos-vm":
        reports.append(prepare_guest_image(runtime))
    elif settings.runtime_backend == "macos-vz":
        from ssios.macos_vz import prepare_macos_vz

        try:
            reports.append(prepare_macos_vz(settings.state_dir, timeout=settings.guest_prepare_timeout_seconds))
        except SsiosError as exc:
            print(json.dumps({"ok": False, "detail": exc.detail}, ensure_ascii=False, indent=2))
            return 1
    elif settings.runtime_agent != "codex":
        from ssios.chat_guest import unsuitable_guest_detail

        print(json.dumps({"ok": False, "detail": unsuitable_guest_detail(settings)}, ensure_ascii=False, indent=2))
        return 1
    ok = all(item.get("ok") for item in reports)
    print(json.dumps({"ok": ok, "steps": reports}, ensure_ascii=False, indent=2), flush=True)
    return 0 if ok else 1


def run_gc(settings, *, dry_run: bool) -> int:
    if dry_run:
        print(json.dumps(collect_leftovers(settings.state_dir, apply=False), ensure_ascii=False, indent=2))
        return 0
    preview = collect_leftovers(settings.state_dir, apply=False)
    print(json.dumps(preview, ensure_ascii=False, indent=2), flush=True)
    if preview["count"] == 0:
        return 0
    print("Type yes to delete these one-shot export copies. The git memory repository will not be deleted.", flush=True)
    line = sys.stdin.readline()
    if line.strip() != "yes":
        print(json.dumps({"result": "pending", "detail": "yes was not entered. No files were deleted."}, ensure_ascii=False))
        return 2
    print(json.dumps(collect_leftovers(settings.state_dir, apply=True), ensure_ascii=False, indent=2))
    return 0


def run_diagnose(settings, task_id: str) -> int:
    import re

    if not re.fullmatch(r"task-[0-9a-f]{16}", task_id):
        print(json.dumps({"error": "task_invalid", "detail": "task id rejected"}, ensure_ascii=False))
        return 1
    path = settings.state_dir / "diagnostics" / f"{task_id}.json"
    if not path.is_file():
        print(json.dumps({"error": "not_found", "detail": "diagnostic file was not found", "path": str(path)}, ensure_ascii=False))
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    content = data.get("content") if isinstance(data.get("content"), str) else ""
    thinking = data.get("thinking") if isinstance(data.get("thinking"), str) else ""
    print(
        json.dumps(
            {
                "path": str(path),
                "classification": data.get("classification"),
                "content_bytes": len(content.encode("utf-8")),
                "thinking_bytes": len(thinking.encode("utf-8")),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


def run_ollama_probe(settings) -> int:
    if settings.llm_provider != "ollama":
        print(
            json.dumps(
                {
                    "error": "probe_not_applicable",
                    "detail": "config llm provider is not ollama; nothing was contacted",
                    "cloud_fallback": False,
                },
                ensure_ascii=False,
            )
        )
        return 2
    report = probe_ollama(
        settings.ollama_base_url,
        settings.ollama_model,
        float(settings.llm_timeout_seconds),
        max_output_tokens=settings.max_output_tokens,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if report.get("error") or not report.get("structured_output"):
        return 1
    return 0


def _up(settings) -> int:
    if production_blocked(settings):
        report = CloudHypervisorRuntime(settings).preflight()
        print(json.dumps({"error": "microvm_fail_closed", "preflight": report}, indent=2, ensure_ascii=False), flush=True)
        return 2
    try:
        plane = ControlPlane(settings)
        plane.open()
        server = ControlServer(plane)
    except SsiosError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail}, ensure_ascii=False), flush=True)
        return 1
    server.start()
    from ssios.config import lan_enabled
    from ssios.netguard import browser_login_urls, local_urls

    urls = local_urls(plane.bound_port, tls=lan_enabled(settings))
    shown = browser_login_urls(plane.bound_port, tls=lan_enabled(settings))
    (settings.state_dir / "listen.url").write_text("\n".join(urls) + "\n", encoding="utf-8")
    print(f"SSIOS dev profile is listening at {shown[0]}", flush=True)
    for extra in shown[1:]:
        print(f"LAN URL: {extra}", flush=True)
    if getattr(server, "fingerprint", ""):
        print(f"Certificate SHA-256 fingerprint: {server.fingerprint}", flush=True)
        print(
            "Compare that fingerprint with the browser warning. "
            "macOS may prompt to allow incoming connections the first time this program listens. "
            "Allow it for browsers on the home network. "
            "If you deny it, a browser on this Mac can still use the loopback address.",
            flush=True,
        )
    print(f"Profile: {settings.profile}, model: {settings.llm_provider}, memory mode: {settings.memory_mode}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
        return 0


def run_upstream_smoke(state: Path, config: str, letta_bin: str) -> int:
    settings = replace(
        load_settings(Path(config), state_dir=state),
        memory_mode="letta-memfs-upstream",
        letta_bin=letta_bin,
    )
    bridge = UpstreamLettaBridge(settings.state_dir, settings.letta_bin)
    try:
        preflight = bridge.preflight()
        before = bridge.core("smoke", budget=4000)
        proposal = bridge.stage_proposal(
            "smoke",
            before["revision"],
            [
                {
                    "path": "topics/fixtures/overview.md",
                    "content": "---\nname: overview\ndescription: \"synthetic upstream topic\"\n---\n\nUPSTREAM-TOPIC-MARKER\n",
                },
                {"path": "topics/fixtures/MEMORY.md", "content": "# Fixtures\n\n- overview.md\n"},
            ],
            task_id="human",
        )
        try:
            bridge.apply(proposal["id"], before["revision"], "unapproved", proposal["payload_sha256"], approved=False)
        except SsiosError as exc:
            if exc.code != "upstream_rejected":
                raise
        revision = bridge.apply(proposal["id"], before["revision"], "cli-smoke", proposal["payload_sha256"], approved=True)
        after = bridge.core("smoke", budget=4000)
        topic = bridge.read_committed("smoke", "topics/fixtures/overview.md")
    except SsiosError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail, "live_integration": False}, indent=2))
        return 1
    report = {
        "live_integration": True,
        "preflight": preflight,
        "core_before": [item["path"] for item in before["files"]],
        "core_after": [item["path"] for item in after["files"]],
        "topics": after["topics"],
        "topic_loaded_as_core": topic["core"],
        "marker_present": "UPSTREAM-TOPIC-MARKER" in topic["content"],
        "revision_changed": revision != before["revision"],
        "fallback": "none",
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not report["marker_present"] or report["topic_loaded_as_core"] or not report["revision_changed"]:
        return 1
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
    try:
        server = ControlServer(plane)
    except SsiosError as exc:
        raise RuntimeError(f"{exc.code}: {exc.detail}") from exc
    server.start()
    base = f"http://127.0.0.1:{plane.bound_port}"
    human = plane.auth.read_secret("human")
    try:
        status, health, _headers = request(base, "GET", "/v1/health", token=human)
        if status != 200 or health.get("llm_provider") != "mock":
            raise RuntimeError("health check failed")
        task = _existing_demo_task(plane)
        if task is None or task["status"] not in ("awaiting_approval", "committed"):
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
        if task["status"] == "awaiting_approval":
            # Demo-only: this process holds the human token. Production `ssios up` never decides.
            code, recovered, _headers = request(base, "POST", f"/v1/tasks/{task['id']}/reissue", token=human, body={})
            if code != 200:
                raise RuntimeError(f"recovery failed: {recovered}")
            if recovered.get("status") == "committed":
                task = recovered
            else:
                task = _approve_pending(base, human, recovered)
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
        print("Demo finished.")
        print(f"Task {task['id']} was approved with the human control token and committed.")
        print(f"Published revision: {task['published_revision']}")
        print(f"Memory revision: {task['memory_revision']}")
        print("Core memory still loads after restart. The topic summary is read by path. The audit log keeps only hashes.")
        return 0
    finally:
        server.stop()


def run_google_command(settings, args) -> int:
    from getpass import getpass

    from ssios.google_connector import member_session

    plane = ControlPlane(settings)
    plane.open()
    session = member_session(args.account)
    google = plane.google
    try:
        if args.command == "oauth-install-client":
            client_id = input("OAuth client id: ").strip()
            client_secret = getpass("OAuth client secret: ")
            print(json.dumps(google.install_client(session, client_id, client_secret), ensure_ascii=False))
            return 0
        if args.command == "oauth-connect":
            started = google.connect(
                session,
                args.connector,
                scope=args.scope or None,
                open_browser=not args.no_browser,
            )
            print("Open this URL in your browser and approve it yourself. SSIOS will not approve it for you.")
            print(started["authorization_url"])
            print(json.dumps(google.finish_connect(session, args.connector), ensure_ascii=False))
            return 0
        if args.command == "oauth-status":
            print(json.dumps(google.status(session), ensure_ascii=False))
            return 0
        if args.command == "oauth-disconnect":
            print(json.dumps(google.disconnect(session, args.connector), ensure_ascii=False))
            return 0
        if args.command == "oauth-revoke-account":
            print(json.dumps(google.revoke_and_delete(args.account), ensure_ascii=False))
            return 0
        from ssios.google_connector import requires_approval

        arguments = json.loads(args.arguments)
        limits = json.loads(args.limits)
        if not requires_approval(args.tool):
            result = google.read_now(
                session,
                project_id=args.project,
                tool=args.tool,
                arguments=arguments,
                limits=limits,
            )
            print(result["text"])
            return 0
        proposed = google.propose_fetch(
            session,
            project_id=args.project,
            tool=args.tool,
            arguments=arguments,
            limits=limits,
        )
        approval = proposed["approval"]
        print(google.preview(approval, session))
        print(json.dumps(approval, ensure_ascii=False))
        print("Type yes to approve this change, or anything else to leave it pending.")
        if input("").strip() != "yes":
            print(json.dumps({"result": "pending", "approval_id": approval["id"]}, ensure_ascii=False))
            return 2
        decided = plane.decide(
            plane.auth.read_secret("human"),
            approval["id"],
            {
                "decision": "approve",
                "action": approval["action"],
                "payload_sha256": approval["payload_sha256"],
                "expected_revision": approval["expected_revision"],
                "bound_hash": approval["bound_hash"],
            },
        )
        result = google.take_result(session, approval["id"])
        print(json.dumps({"approval_status": decided.get("status"), "text": result["text"]}, ensure_ascii=False))
        return 0
    except SsiosError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail}, ensure_ascii=False))
        return 1


def _existing_demo_task(plane) -> dict | None:
    matches = [
        task
        for task in plane.tasks.values()
        if task.get("project_id") == "demo-alpha" and task.get("kind") == "project-summary"
    ]
    for status in ("awaiting_approval", "committed"):
        found = [task for task in matches if task.get("status") == status]
        if found:
            return found[-1]
    return None


def _approve_pending(base: str, human: str, task: dict) -> dict:
    for approval_id in task["approval_ids"]:
        code, approval, _headers = request(base, "GET", f"/v1/approvals/{approval_id}", token=human)
        if code != 200:
            raise RuntimeError(f"preview failed: {approval}")
        status_name = approval.get("status")
        if status_name == "consumed" and approval.get("decision") == "approve":
            continue
        if status_name == "expired":
            continue
        if status_name != "pending":
            raise RuntimeError(f"approval is not pending: {status_name}")
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
    return _poll(base, human, task["id"], {"committed", "failed", "rejected"})


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
