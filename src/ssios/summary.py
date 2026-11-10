"""Run one synthetic project-summary against an already running service.

This command never starts a second service and never approves by itself.
The human must type yes. Demo auto-approval stays inside `ssios demo`.
"""

from __future__ import annotations

import difflib
import json
import sys
import time
import urllib.error
import urllib.parse

from ssios.auth import AuthStore
from ssios.client import request
from ssios.errors import SsiosError

SUMMARY_PROJECT = "local-summary"
SUMMARY_INPUT = "notes.md"
SUMMARY_REVISION = "exp-1"
TOPIC_PATH = "topics/fixtures/last-summary.md"
CONFIRM = "Confirm: type yes to approve, or no to deny."


def run_summary(settings, *, project_id: str = SUMMARY_PROJECT, topic: str = "") -> int:
    token = _human_token(settings)
    base = _base_url(settings)
    _require_identity(settings, base, token)
    created = _create_task(base, token, project_id, topic or project_id)
    task = _poll(
        base,
        token,
        created["id"],
        {"awaiting_approval", "failed", "timeout", "cancelled", "rejected"},
        timeout=float(settings.task_timeout_seconds) + 5,
    )
    if task.get("status") != "awaiting_approval":
        _emit(
            {
                "error": str(task.get("error_code") or task.get("status") or "task_failed"),
                "detail": _failure_detail(task),
                "task_id": task.get("id"),
                "retry": "new-task",
                "diagnostic_path": task.get("diagnostic_path"),
                "diagnostic_class": task.get("diagnostic_class"),
            }
        )
        return 1
    pending = _show_pending(base, token, task)
    print(CONFIRM, flush=True)
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        _emit({"result": "pending", "task_id": task["id"], "detail": "Interrupted. Nothing was approved."})
        return 130
    if line == "":
        _emit({"result": "pending", "task_id": task["id"], "detail": "No input. The approval stays pending and was not approved."})
        return 2
    choice = line.strip()
    decide_timeout = max(60.0, float(settings.llm_timeout_seconds))
    if choice == "no":
        for approval in pending:
            _decide(base, token, approval, "deny", decide_timeout)
        final = _poll(
            base,
            token,
            task["id"],
            {"rejected", "failed", "committed", "timeout", "cancelled"},
            timeout=10,
        )
        _emit({"result": "denied", "task_id": task["id"], "status": final.get("status")})
        return 0
    if choice != "yes":
        _emit({"result": "pending", "task_id": task["id"], "detail": "yes was not entered. The approval stays pending and was not approved."})
        return 2
    for approval in pending:
        _decide(base, token, approval, "approve", decide_timeout)
    final = _poll(
        base,
        token,
        task["id"],
        {"committed", "rejected", "failed", "timeout", "cancelled"},
        timeout=10,
    )
    if final.get("status") != "committed":
        raise SsiosError(str(final.get("error_code") or final.get("status") or "task_failed"), _failure_detail(final))
    return _verify(base, token, final)


def _human_token(settings) -> str:
    path = settings.state_dir / "secrets" / "human.token"
    if not path.is_file():
        raise SsiosError(
            "state_not_ready",
            "secrets/human.token is missing from the state directory. Run ssios up with the same --state and --config first. Do not delete existing state.",
        )
    try:
        token = AuthStore(settings.state_dir).read_secret("human")
    except SsiosError:
        raise
    except OSError as exc:
        raise SsiosError("state_not_ready", "The human token file cannot be read") from exc
    if not token:
        raise SsiosError("state_not_ready", "The human token file is empty")
    return token


def _base_url(settings) -> str:
    host = "127.0.0.1" if settings.host == "localhost" else settings.host
    return f"http://{host}:{settings.port}"


def _require_identity(settings, base: str, token: str) -> None:
    code, payload, _headers = _call(base, "GET", "/v1/status", token=token, timeout=5)
    if code == 401:
        raise SsiosError("unauthorized", "The running service rejected the token. Confirm --state is that service's state directory. The token is not printed.")
    if code != 200:
        raise SsiosError(str(payload.get("error") or "status_failed"), str(payload.get("detail") or "status failed"))
    actual = payload.get("identity")
    if not isinstance(actual, dict):
        raise SsiosError(
            "service_identity_missing",
            "The running service has no identity field. After updating the code, restart that one process with the original --state and --config. Do not delete the state directory, and do not start a second service.",
        )
    expected = {
        "state_dir": str(settings.state_dir),
        "config_path": str(settings.config_path or ""),
        "profile": settings.profile,
        "memory_mode": settings.memory_mode,
        "llm_provider": settings.llm_provider,
        "ollama_model": settings.ollama_model,
        "ollama_base_url": settings.ollama_base_url,
        "llm_timeout_seconds": settings.llm_timeout_seconds,
        "max_output_tokens": settings.max_output_tokens,
        "num_ctx": settings.num_ctx,
        "summary_repair_retries": settings.summary_repair_retries,
        "max_note_chars": settings.max_note_chars,
        "task_timeout_seconds": settings.task_timeout_seconds,
        "letta_bin": settings.letta_bin,
        "bind": f"{settings.host}:{settings.port}",
    }
    mismatched = [key for key, value in expected.items() if not _same(key, value, actual.get(key))]
    if not mismatched:
        return
    quiet = {"letta_bin", "state_dir", "config_path"}
    parts = []
    for key in mismatched:
        if key in quiet:
            parts.append(key)
        else:
            parts.append(f"{key} config={expected[key]!r} service={actual.get(key)!r}")
    raise SsiosError(
        "service_mismatch",
        "The config does not match the running service: "
        + "; ".join(parts)
        + ". Stop the old process and start it again with the same --state and --config. This command will not start another service and will not change memory.",
    )


def _same(key: str, expected, actual) -> bool:
    if key in (
        "llm_timeout_seconds",
        "task_timeout_seconds",
        "max_output_tokens",
        "num_ctx",
        "summary_repair_retries",
        "max_note_chars",
    ):
        try:
            return float(expected) == float(actual)
        except (TypeError, ValueError):
            return False
    return expected == actual


def _create_task(base: str, token: str, project_id: str, topic: str) -> dict:
    code, payload, _headers = _call(
        base,
        "POST",
        "/v1/tasks",
        token=token,
        body={
            "project_id": project_id,
            "topic": topic,
            "kind": "project-summary",
            "input_revision": SUMMARY_REVISION,
            "input_path": SUMMARY_INPUT,
        },
        timeout=10,
    )
    if code == 202 and payload.get("id"):
        return payload
    detail = str(payload.get("detail") or "task create failed")
    if payload.get("error") == "not_found":
        detail = (
            "The local-summary export is not in this service yet. "
            "Restart the running ssios up with the same --state and --config so it adds that export. Do not delete existing state."
        )
    raise SsiosError(str(payload.get("error") or "task_create_failed"), detail)


def _show_pending(base: str, token: str, task: dict) -> list[dict]:
    pending = []
    for approval_id in task.get("approval_ids") or []:
        code, approval, _headers = _call(base, "GET", f"/v1/approvals/{approval_id}", token=token, timeout=10)
        if code != 200:
            raise SsiosError(str(approval.get("error") or "preview_failed"), str(approval.get("detail") or "preview failed"))
        if approval.get("status") != "pending":
            continue
        print(
            f"Pending {approval.get('action')} payload {approval.get('payload_sha256')} expected revision {approval.get('expected_revision')}",
            flush=True,
        )
        for item in approval.get("files") or []:
            path = str(item.get("path") or "")
            after = str(item.get("content") or "")
            before = _current_text(base, token, task["project_id"], approval.get("action"), path)
            _print_diff(path, before, after)
        if not approval.get("files") and approval.get("preview"):
            print(str(approval.get("preview")), flush=True)
        pending.append(approval)
    if not pending:
        raise SsiosError("approval_missing", "The task is waiting for approval, but no approval is pending.")
    return pending


def _current_text(base: str, token: str, project_id: str, action: str, path: str) -> str | None:
    if action == "storage.publish":
        code, payload, _headers = _call(
            base,
            "GET",
            f"/v1/storage/published?project_id={urllib.parse.quote(project_id)}&revision=HEAD",
            token=token,
            timeout=10,
        )
    else:
        code, payload, _headers = _call(
            base,
            "GET",
            f"/v1/memory/file?project_id={urllib.parse.quote(project_id)}&path={urllib.parse.quote(path, safe='')}",
            token=token,
            timeout=10,
        )
    if code == 200 and isinstance(payload, dict):
        content = payload.get("content")
        return content if isinstance(content, str) else None
    return None


def _print_diff(path: str, before: str | None, after: str) -> None:
    if before is None:
        print(f"--- /dev/null", flush=True)
        print(f"+++ {path}", flush=True)
        for line in after.splitlines() or [""]:
            print(f"+{line}", flush=True)
        return
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile=path, tofile=path, lineterm="")
    text = "\n".join(diff)
    print(text or f"{path} is unchanged", flush=True)


def _decide(base: str, token: str, approval: dict, decision: str, timeout: float) -> None:
    code, payload, _headers = _call(
        base,
        "POST",
        f"/v1/approvals/{approval['id']}/decide",
        token=token,
        body={
            "decision": decision,
            "action": approval["action"],
            "payload_sha256": approval["payload_sha256"],
            "expected_revision": approval["expected_revision"],
            "bound_hash": approval["bound_hash"],
        },
        timeout=timeout,
    )
    if code != 200:
        raise SsiosError(str(payload.get("error") or "approval_failed"), str(payload.get("detail") or "approval failed"))


def _verify(base: str, token: str, task: dict) -> int:
    project = urllib.parse.quote(str(task.get("project_id") or SUMMARY_PROJECT))
    code, published, _headers = _call(
        base,
        "GET",
        f"/v1/storage/published?project_id={project}&revision=HEAD",
        token=token,
        timeout=10,
    )
    code2, topic, _headers = _call(
        base,
        "GET",
        f"/v1/memory/file?project_id={project}&path={urllib.parse.quote(TOPIC_PATH, safe='')}",
        token=token,
        timeout=30,
    )
    code3, core, _headers = _call(base, "GET", f"/v1/memory/core?project_id={project}", token=token, timeout=30)
    code4, status, _headers = _call(base, "GET", "/v1/status", token=token, timeout=5)
    if code != 200 or code2 != 200 or code3 != 200 or code4 != 200:
        raise SsiosError("verify_failed", "The published result or memory could not be read after approval")
    if published.get("sha256") != task.get("output_sha256"):
        raise SsiosError("verify_failed", "The published content does not match the task payload")
    if published.get("content") not in str(topic.get("content") or ""):
        raise SsiosError("verify_failed", "The committed topic memory does not contain this published summary")
    if topic.get("core") is not False:
        raise SsiosError("verify_failed", "The topic summary was loaded into core memory")
    core_paths = [str(item.get("path")) for item in core.get("files") or [] if isinstance(item, dict)]
    if TOPIC_PATH in core_paths:
        raise SsiosError("verify_failed", "The topic path is in the core file list")
    memory = status.get("memory") if isinstance(status.get("memory"), dict) else {}
    llm = status.get("llm") if isinstance(status.get("llm"), dict) else {}
    runtime = status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
    backend = str(runtime.get("active_backend") or "dev-guard")
    print("A reachable model is not a successful task. The published result is below.", flush=True)
    if task.get("isolated"):
        print(
            f"Runtime backend is {task.get('runtime_backend') or backend}. The guest ran, and boot plus completion took {task.get('boot_ms')} ms. The model call stayed on the host.",
            flush=True,
        )
    elif backend == "dev-guard":
        print("Runtime backend is dev-guard: the task finished inside the control-plane process. No microVM was started.", flush=True)
    else:
        print(f"Runtime backend is {backend}, but the guest did not run. The task did not finish inside an isolated virtual machine.", flush=True)
    print(f"Task {task['id']} is committed.", flush=True)
    print(f"Published {published.get('revision')} sha256 {published.get('sha256')}", flush=True)
    print(
        f"Memory {TOPIC_PATH} trust={topic.get('trust')} core={topic.get('core')} revision={topic.get('revision')}",
        flush=True,
    )
    print("Core files: " + (", ".join(core_paths) if core_paths else "(none)"), flush=True)
    print(
        f"Service memory={memory.get('mode') or status.get('memory_mode')} provider={llm.get('provider')} model={llm.get('model')}",
        flush=True,
    )
    _emit(
        {
            "result": "committed",
            "task_id": task["id"],
            "project_id": task.get("project_id") or SUMMARY_PROJECT,
            "published_revision": published.get("revision"),
            "memory_revision": topic.get("revision"),
            "memory_trust": topic.get("trust"),
            "topic_core": topic.get("core"),
            "output_sha256": task.get("output_sha256"),
        }
    )
    return 0


def _poll(base: str, token: str, task_id: str, terminal: set[str], timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        code, last, _headers = _call(base, "GET", f"/v1/tasks/{task_id}", token=token, timeout=10)
        if code != 200:
            raise SsiosError(str(last.get("error") or "task_poll_failed"), str(last.get("detail") or "task poll failed"))
        if last.get("status") in terminal:
            return last
        time.sleep(0.1)
    raise SsiosError("task_timeout", "Timed out waiting for the task to finish. The task on the service is unchanged and was not auto-approved.")


def _failure_detail(task: dict) -> str:
    code = str(task.get("error_code") or task.get("status") or "task_failed")
    if code == "llm_unavailable":
        return "The model did not finish this task. An Ollama command that answers only shows that the process is listening. It does not mean the task succeeded. There is no cloud fallback."
    if code == "model_output_invalid":
        detail = (
            "The model output was not the required JSON. Thinking text was not treated as the summary and was not approved. "
            "This failed task is kept. Running ssios summary again creates a new task and does not delete existing state."
        )
        if task.get("diagnostic_path"):
            detail += f" Diagnostic file: {task['diagnostic_path']} ({task.get('diagnostic_class')}). Use ssios diagnose {task.get('id')} to see the classification."
        return detail
    if code == "model_output_empty":
        return (
            "The model returned no usable answer. Text inside message.thinking and think tags was not executed. "
            "This failed task is kept. Running ssios summary again creates a new task."
        )
    if code == "model_output_truncated":
        return (
            "The model output was truncated and did not reach approval. Raise max_output_tokens under [llm], restart the service with the same --state and --config, then run ssios summary again. "
            "The failure record is kept, and a retry creates a new task."
        )
    if code == "no_content":
        return (
            "There are no notes to summarize. The default human.md or persona.md template is not treated as content. "
            "Import md or txt with ssios ingest, approve it, then run ssios summary. Existing state was not deleted."
        )
    if code == "model_output_low_quality":
        return (
            "The model summary only restated internal fields, truncated JSON, or was too short to use, so no approval was created. "
            "Running ssios summary again creates a new task. If a bad summary was already approved, a new yes makes one new git commit that replaces topics/fixtures/last-summary.md. It does not delete the state directory or rewrite old history."
        )
    if code == "model_format_unsupported":
        return "This Ollama does not accept a structured format. Upgrade the local Ollama. Nothing was sent to the cloud, and state was not deleted."
    if code == "llm_response_unsupported":
        return "The Ollama response shape is not supported. Unknown fields were not treated as approval. The failure record is kept, and a retry creates a new task."
    if code == "microvm_fail_closed":
        detail = "The guest did not finish this task, so it was not rerun on the host and nothing was approved."
        if task.get("error_detail"):
            detail += " " + str(task["error_detail"])
        if task.get("diagnostic_path"):
            detail += f" Diagnostic file: {task['diagnostic_path']}."
        return detail
    if code in ("timeout", "task_timeout"):
        return "The task timed out. Raise limits.task_timeout_seconds, restart the service with the same --state and --config, and try again. Do not delete state."
    return f"The task did not reach awaiting approval: {code}"


def _call(base: str, method: str, path: str, *, token: str, body: dict | None = None, timeout: float = 10):
    try:
        return request(base, method, path, token=token, body=body, timeout=timeout)
    except json.JSONDecodeError as exc:
        raise SsiosError("bad_response", "The service did not return JSON") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SsiosError(
            "service_unavailable",
            f"Cannot reach {base}. Start the existing service with the same --state and --config. This command will not start another service.",
        ) from exc


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
