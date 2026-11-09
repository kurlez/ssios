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
CONFIRM = "确认：输入 yes 批准，输入 no 拒绝。"


def run_summary(settings) -> int:
    token = _human_token(settings)
    base = _base_url(settings)
    _require_identity(settings, base, token)
    created = _create_task(base, token)
    task = _poll(
        base,
        token,
        created["id"],
        {"awaiting_approval", "failed", "timeout", "cancelled", "rejected"},
        timeout=float(settings.task_timeout_seconds) + 5,
    )
    if task.get("status") != "awaiting_approval":
        raise SsiosError(str(task.get("error_code") or task.get("status") or "task_failed"), _failure_detail(task))
    pending = _show_pending(base, token, task)
    print(CONFIRM, flush=True)
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        _emit({"result": "pending", "task_id": task["id"], "detail": "已中断，没有批准。"})
        return 130
    if line == "":
        _emit({"result": "pending", "task_id": task["id"], "detail": "没有输入，审批保持待处理，没有批准。"})
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
        _emit({"result": "pending", "task_id": task["id"], "detail": "未输入 yes，审批保持待处理，没有批准。"})
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
            "状态目录里没有 secrets/human.token。请先用同一 --state 和 --config 运行 ssios up。不要删除已有状态。",
        )
    try:
        token = AuthStore(settings.state_dir).read_secret("human")
    except SsiosError:
        raise
    except OSError as exc:
        raise SsiosError("state_not_ready", "无法读取人类令牌文件") from exc
    if not token:
        raise SsiosError("state_not_ready", "人类令牌文件是空的")
    return token


def _base_url(settings) -> str:
    host = "127.0.0.1" if settings.host == "localhost" else settings.host
    return f"http://{host}:{settings.port}"


def _require_identity(settings, base: str, token: str) -> None:
    code, payload, _headers = _call(base, "GET", "/v1/status", token=token, timeout=5)
    if code == 401:
        raise SsiosError("unauthorized", "令牌不被正在运行的服务接受。确认 --state 就是该服务的状态目录。令牌不会打印。")
    if code != 200:
        raise SsiosError(str(payload.get("error") or "status_failed"), str(payload.get("detail") or "status failed"))
    actual = payload.get("identity")
    if not isinstance(actual, dict):
        raise SsiosError(
            "service_identity_missing",
            "正在运行的服务没有身份字段。更新代码后，用原来的 --state 和 --config 重启那一个进程。不要删除状态目录，也不要另开第二个服务。",
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
        "配置与正在运行的服务不一致："
        + "; ".join(parts)
        + "。停掉旧进程后用同一 --state 和 --config 重新启动。这条命令不会另开服务，也不会改记忆。",
    )


def _same(key: str, expected, actual) -> bool:
    if key in ("llm_timeout_seconds", "task_timeout_seconds"):
        try:
            return float(expected) == float(actual)
        except (TypeError, ValueError):
            return False
    return expected == actual


def _create_task(base: str, token: str) -> dict:
    code, payload, _headers = _call(
        base,
        "POST",
        "/v1/tasks",
        token=token,
        body={
            "project_id": SUMMARY_PROJECT,
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
            "合成项目 local-summary 的导出还不在这个服务里。"
            "用同一 --state 和 --config 重启正在运行的 ssios up，让它补上这份导出。不要删除已有状态。"
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
            f"待确认 {approval.get('action')} 载荷 {approval.get('payload_sha256')} 期望修订 {approval.get('expected_revision')}",
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
        raise SsiosError("approval_missing", "任务在等待批准，但没有待处理的审批。")
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
    print(text or f"{path} 内容没有变化", flush=True)


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
    project = urllib.parse.quote(SUMMARY_PROJECT)
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
        raise SsiosError("verify_failed", "批准后的发布或记忆读取失败")
    if published.get("sha256") != task.get("output_sha256"):
        raise SsiosError("verify_failed", "发布内容与任务载荷不一致")
    if published.get("content") not in str(topic.get("content") or ""):
        raise SsiosError("verify_failed", "已提交的主题记忆里没有这次发布的摘要")
    if topic.get("core") is not False:
        raise SsiosError("verify_failed", "主题摘要进入了核心记忆")
    core_paths = [str(item.get("path")) for item in core.get("files") or [] if isinstance(item, dict)]
    if TOPIC_PATH in core_paths:
        raise SsiosError("verify_failed", "主题路径出现在核心文件列表里")
    memory = status.get("memory") if isinstance(status.get("memory"), dict) else {}
    llm = status.get("llm") if isinstance(status.get("llm"), dict) else {}
    print("模型连通不等于任务成功。下面是已发布结果。", flush=True)
    print("microVM 没有执行这次任务。", flush=True)
    print(f"任务 {task['id']} 已提交。", flush=True)
    print(f"发布 {published.get('revision')} sha256 {published.get('sha256')}", flush=True)
    print(
        f"记忆 {TOPIC_PATH} trust={topic.get('trust')} core={topic.get('core')} revision={topic.get('revision')}",
        flush=True,
    )
    print("核心文件：" + (", ".join(core_paths) if core_paths else "(无)"), flush=True)
    print(
        f"服务 memory={memory.get('mode') or status.get('memory_mode')} provider={llm.get('provider')} model={llm.get('model')}",
        flush=True,
    )
    _emit(
        {
            "result": "committed",
            "task_id": task["id"],
            "project_id": SUMMARY_PROJECT,
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
    raise SsiosError("task_timeout", "等待任务结束超时。服务上的任务保持原状，没有自动批准。")


def _failure_detail(task: dict) -> str:
    code = str(task.get("error_code") or task.get("status") or "task_failed")
    if code == "llm_unavailable":
        return "模型没有完成这次任务。Ollama 命令能回答只说明进程在听，不代表任务成功。没有云端回退。"
    if code == "model_output_invalid":
        return "模型输出不是要求的 JSON。思考过程里的 approve 不会被当成批准。"
    if code in ("timeout", "task_timeout"):
        return "任务超时。可以加大 limits.task_timeout_seconds 后，用同一 --state 和 --config 重启服务再试。不要删除状态。"
    return f"任务没有进入待批准：{code}"


def _call(base: str, method: str, path: str, *, token: str, body: dict | None = None, timeout: float = 10):
    try:
        return request(base, method, path, token=token, body=body, timeout=timeout)
    except json.JSONDecodeError as exc:
        raise SsiosError("bad_response", "服务返回的不是 JSON") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SsiosError(
            "service_unavailable",
            f"连不上 {base}。请用同一 --state 和 --config 启动已有服务。这条命令不会另开一个服务。",
        ) from exc


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
