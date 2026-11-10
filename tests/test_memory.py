"""Letta MemFS-compatible store: provenance, CAS, deletion, symlink, and budget."""

from __future__ import annotations

import json
import os
import threading

from support import PlaneTest, wait_task

from ssios.memory_store import worktree_has_phrase


class MemoryFlowTest(PlaneTest):
    def test_topic_is_not_in_core_and_skill_is_on_demand(self) -> None:
        status, core, _headers = self.call("GET", "/v1/memory/core?project_id=demo-alpha", token=self.human)
        self.assertEqual(status, 200)
        blob = json.dumps(core)
        self.assertEqual(core["mode"], "letta-memfs-compat")
        self.assertIn("CORE-PROJECT-FACT", blob)
        self.assertNotIn("THREE-BULLET-RULE", blob)
        self.assertNotIn("SKILL-ON-DEMAND-ONLY", blob)
        self.assertNotIn("BETA-ONLY-MEMORY-PHRASE", blob)
        status, skill, _headers = self.call(
            "GET",
            "/v1/memory/file?project_id=demo-alpha&path=skills/fixture-summary/SKILL.md",
            token=self.human,
        )
        self.assertEqual(status, 200)
        self.assertIn("SKILL-ON-DEMAND-ONLY", skill["content"])
        self.assertEqual(skill["trust"], "synthetic_seed")

    def test_search_is_keyword_and_does_not_log_the_query(self) -> None:
        status, body, _headers = self.call(
            "GET",
            "/v1/memory/search?project_id=demo-alpha&q=SYNTHETIC-VAULT-CANARY",
            token=self.human,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["search_mode"], "keyword")
        self.assertEqual(body["hits"], [])
        audit = (self.state / "audit" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("SYNTHETIC-VAULT-CANARY", audit)
        self.assertIn("query_sha256", audit)
        status, body, _headers = self.call(
            "GET",
            "/v1/memory/search?project_id=demo-alpha&q=CORE-PROJECT-FACT",
            token=self.human,
        )
        self.assertEqual(status, 200)
        self.assertTrue(any(hit["path"] == "project.md" for hit in body["hits"]))

    def test_cas_conflict_and_pending_proposal_is_not_canonical(self) -> None:
        status, core, _headers = self.call("GET", "/v1/memory/core?project_id=demo-alpha", token=self.human)
        revision = core["revision"]
        first = self._propose(revision, "topics/fixtures/one.md", "FIRST-MARKER")
        second = self._propose(revision, "topics/fixtures/two.md", "SECOND-MARKER")
        hidden, hidden_body, _headers = self.call(
            "GET",
            "/v1/memory/file?project_id=demo-alpha&path=topics/fixtures/one.md",
            token=self.human,
        )
        self.assertEqual(hidden, 404)
        self.assertNotIn("FIRST-MARKER", json.dumps(hidden_body))
        ok, applied, _headers = self.call(
            "POST",
            f"/v1/approvals/{first['id']}/decide",
            token=self.human,
            body=self._body(first),
        )
        self.assertEqual(ok, 200, applied)
        conflict, body, _headers = self.call(
            "POST",
            f"/v1/approvals/{second['id']}/decide",
            token=self.human,
            body=self._body(second),
        )
        self.assertEqual(conflict, 409)
        self.assertEqual(body["error"], "cas_conflict")
        status, winner, _headers = self.call(
            "GET",
            "/v1/memory/file?project_id=demo-alpha&path=topics/fixtures/one.md",
            token=self.human,
        )
        self.assertEqual(status, 200)
        self.assertEqual(winner["trust"], "approved")
        self.assertIn("FIRST-MARKER", winner["content"])
        status, loser, _headers = self.call(
            "GET",
            "/v1/memory/file?project_id=demo-alpha&path=topics/fixtures/two.md",
            token=self.human,
        )
        self.assertEqual(status, 404)

    def test_concurrent_approvals_only_one_revision_wins(self) -> None:
        _status, core, _headers = self.call("GET", "/v1/memory/core?project_id=demo-alpha", token=self.human)
        first = self._propose(core["revision"], "topics/fixtures/race-one.md", "RACE-ONE")
        second = self._propose(core["revision"], "topics/fixtures/race-two.md", "RACE-TWO")
        results: list[tuple[int, str | None]] = []

        def decide(approval: dict) -> None:
            code, body, _headers = self.call(
                "POST",
                f"/v1/approvals/{approval['id']}/decide",
                token=self.human,
                body=self._body(approval),
            )
            results.append((code, body.get("error")))

        threads = [threading.Thread(target=decide, args=(approval,)) for approval in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(code for code, _error in results), [200, 409])
        self.assertIn("cas_conflict", [error for _code, error in results])

    def test_storage_publish_cas(self) -> None:
        first_id = "task-" + "ab" * 8
        second_id = "task-" + "cd" * 8
        first = self.plane.storage.stage(first_id, "summary.md", b"one\n")
        second = self.plane.storage.stage(second_id, "summary.md", b"two\n")
        self.plane.storage.publish("demo-alpha", first_id, "none", first["sha256"])
        with self.assertRaises(Exception) as caught:
            self.plane.storage.publish("demo-alpha", second_id, "none", second["sha256"])
        self.assertEqual(caught.exception.code, "cas_conflict")
        published = self.plane.storage.read_published("demo-alpha", "HEAD")
        self.assertEqual(published["content"], "one\n")
        self.assertNotIn("two", published["content"])

    def test_forget_hides_worktree_but_git_history_retains_the_phrase(self) -> None:
        status, core, _headers = self.call("GET", "/v1/memory/core?project_id=demo-alpha", token=self.human)
        code, proposal, _headers = self.call(
            "POST",
            "/v1/memory/forget",
            token=self.human,
            body={
                "project_id": "demo-alpha",
                "expected_revision": core["revision"],
                "path": "topics/fixtures/obsolete.md",
            },
        )
        self.assertEqual(code, 200, proposal)
        approval = proposal["approval"]
        code, result, _headers = self.call(
            "POST",
            f"/v1/approvals/{approval['id']}/decide",
            token=self.human,
            body=self._body(approval),
        )
        self.assertEqual(code, 200, result)
        code, tombstone, _headers = self.call(
            "GET",
            "/v1/memory/file?project_id=demo-alpha&path=topics/fixtures/obsolete.md",
            token=self.human,
        )
        self.assertEqual(code, 200)
        self.assertTrue(tombstone["tombstone"])
        self.assertIsNone(tombstone["content"])
        self.assertNotIn("FORGET-ME-PHRASE", json.dumps(tombstone))
        code, found, _headers = self.call(
            "GET",
            "/v1/memory/search?project_id=demo-alpha&q=FORGET-ME-PHRASE",
            token=self.human,
        )
        self.assertEqual(found["hits"], [])
        code, core_after, _headers = self.call("GET", "/v1/memory/core?project_id=demo-alpha", token=self.human)
        self.assertNotIn("FORGET-ME-PHRASE", json.dumps(core_after))
        repo = self.plane.memory.repo("demo-alpha")
        self.assertFalse(worktree_has_phrase(repo, "FORGET-ME-PHRASE"))
        history = self.plane.memory.git.run(repo, ["log", "-p", "--all"]).stdout
        self.assertIn(b"FORGET-ME-PHRASE", history)

    def test_symlink_and_untrusted_worktree_do_not_leak(self) -> None:
        repo = self.plane.memory.repo("demo-alpha")
        vault = self.state / "vault" / "canary.txt"
        os.symlink(vault, repo / "topics" / "fixtures" / "link.md")
        code, body, _headers = self.call(
            "GET",
            "/v1/memory/file?project_id=demo-alpha&path=topics/fixtures/link.md",
            token=self.human,
        )
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "symlink_rejected")
        self.assertNotIn("SYNTHETIC-VAULT-CANARY", json.dumps(body))
        rogue = repo / "rogue.md"
        rogue.write_text('---\nname: rogue\ndescription: "x"\n---\n\nROGUE-PHRASE\n', encoding="utf-8")
        self.plane.memory.git.commit_paths(repo, ["rogue.md"], "untrusted direct write")
        token = self.plane.issue_task_token("demo-alpha", paths=("notes.md",), revision="exp-1")
        code, body, _headers = self.call(
            "GET",
            "/v1/memory/file?project_id=demo-alpha&path=rogue.md",
            token=token,
        )
        self.assertEqual(code, 403)
        self.assertEqual(body["error"], "untrusted_source")
        self.assertNotIn("ROGUE-PHRASE", json.dumps(body))
        code, human_body, _headers = self.call(
            "GET",
            "/v1/memory/file?project_id=demo-alpha&path=rogue.md",
            token=self.human,
        )
        self.assertEqual(code, 200)
        self.assertEqual(human_body["trust"], "unknown")
        code, core, _headers = self.call("GET", "/v1/memory/core?project_id=demo-alpha", token=token)
        self.assertNotIn("ROGUE-PHRASE", json.dumps(core))

    def test_export_symlink_and_byte_tamper(self) -> None:
        export = self.state / "storage" / "exports" / "demo-alpha" / "exp-1"
        os.symlink(self.state / "vault" / "canary.txt", export / "linked.md")
        manifest_path = export / "manifest.json"
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["linked.md"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        code, body, _headers = self.call(
            "GET",
            "/v1/storage/input?project_id=demo-alpha&revision=exp-1&path=linked.md",
            token=self.human,
        )
        self.assertEqual(code, 400)
        self.assertEqual(body["error"], "symlink_rejected")
        self.assertNotIn("SYNTHETIC-VAULT-CANARY", json.dumps(body))
        notes = export / "notes.md"
        notes.chmod(0o644)
        notes.write_bytes(notes.read_bytes().replace(b"NORTHWIND", b"NORTHW1ND"))
        code, body, _headers = self.call(
            "GET",
            "/v1/storage/input?project_id=demo-alpha&revision=exp-1&path=notes.md",
            token=self.human,
        )
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "export_integrity")
        self.assertNotIn("NORTHWIND", json.dumps(body))

    def _propose(self, revision: str, path: str, marker: str) -> dict:
        content = f'---\nname: note\ndescription: "synthetic"\n---\n\n{marker}\n'
        code, body, _headers = self.call(
            "POST",
            "/v1/memory/propose",
            token=self.human,
            body={
                "project_id": "demo-alpha",
                "expected_revision": revision,
                "files": [{"path": path, "content": content}],
            },
        )
        self.assertEqual(code, 200, body)
        return body["approval"]

    @staticmethod
    def _body(approval: dict) -> dict:
        return {
            "decision": "approve",
            "action": approval["action"],
            "payload_sha256": approval["payload_sha256"],
            "expected_revision": approval["expected_revision"],
            "bound_hash": approval["bound_hash"],
        }


class BudgetTest(PlaneTest):
    settings_overrides = {"core_token_budget": 32}

    def test_core_budget_truncates(self) -> None:
        code, core, _headers = self.call("GET", "/v1/memory/core?project_id=demo-alpha", token=self.human)
        self.assertEqual(code, 200)
        self.assertTrue(core["truncated"])
        self.assertLessEqual(core["token_estimate"], 32)
        self.assertTrue(core["omitted"])


class StagingTest(PlaneTest):
    def test_staged_tamper_and_refreeze(self) -> None:
        code, created, _headers = self.call(
            "POST",
            "/v1/tasks",
            token=self.human,
            body={
                "project_id": "demo-alpha",
                "kind": "project-summary",
                "input_revision": "exp-1",
                "input_path": "notes.md",
            },
        )
        self.assertEqual(code, 202, created)
        task = wait_task(self.base, self.human, created["id"], {"awaiting_approval", "failed"})
        self.assertEqual(task["status"], "awaiting_approval", task)
        token = self.plane._tokens[task["id"]]
        with self.assertRaises(Exception) as caught:
            self.plane.stage_again(token, task["id"], b"changed\n")
        self.assertEqual(caught.exception.code, "staging_frozen")
        staged = self.state / "storage" / "tasks" / task["id"] / "staged" / "summary.md"
        approval = next(item for item in task["approvals"] if item["action"] == "storage.publish")
        staged.write_text("tampered summary\n", encoding="utf-8")
        code, body, _headers = self.call(
            "POST",
            f"/v1/approvals/{approval['id']}/decide",
            token=self.human,
            body={
                "decision": "approve",
                "action": approval["action"],
                "payload_sha256": approval["payload_sha256"],
                "expected_revision": approval["expected_revision"],
                "bound_hash": approval["bound_hash"],
            },
        )
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "payload_mismatch")
        self.assertEqual(self.plane.storage.published_head("demo-alpha"), "none")
