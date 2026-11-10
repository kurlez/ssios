"""Pinned facts about upstream Letta MemFS.

Letta MemFS is the git-backed memory checkout used by Letta Code. As of the
upstream package inspected for this prototype, it is not a standalone library
that the trusted control plane can import without the shell-capable harness.
"""

from __future__ import annotations

import os
import shutil
import subprocess

UPSTREAM_LETTA_CODE_VERSION = "0.34.45"
UPSTREAM_LICENSE = "Apache-2.0"
UPSTREAM_REPO = "https://github.com/letta-ai/letta-code"
UPSTREAM_DOCS = "https://docs.letta.com/letta-code/memfs"
UPSTREAM_PACKAGE = "@letta-ai/letta-code"
LOCAL_MEMORY_LAYOUT = "~/.letta/lc-local-backend/memfs/<agent-id>/memory"


def describe_memfs(mode: str = "letta-memfs-compat") -> dict:
    upstream = mode == "letta-memfs-upstream"
    return {
        "mode": mode,
        "label": "letta-code-local-cli" if upstream else "local-compatibility",
        "letta_code_embedded": False,
        "npm_memfs_package": False,
        "vector_store": False,
        "semantic_index": False,
        "canonical_store": "letta-local-memfs-git" if upstream else "git-markdown-compat",
        "fallback_to_compat": False,
        "harness_prompt": False,
        "pinned_upstream_package": UPSTREAM_PACKAGE,
        "pinned_upstream_version": UPSTREAM_LETTA_CODE_VERSION,
        "pinned_upstream_license": UPSTREAM_LICENSE,
        "pinned_upstream_repo": UPSTREAM_REPO,
        "docs": UPSTREAM_DOCS,
        "upstream_local_layout": LOCAL_MEMORY_LAYOUT,
        "upstream_tasks_outstanding": [
            "letta -p agent turns are intentionally not invoked",
            "official git smart HTTP memory sidecar is not bundled",
        ],
    }


def probe_local_binary(*, execute: bool = False) -> dict:
    path = shutil.which("letta")
    result = {
        "binary_found": path is not None,
        "executed": False,
        "embedded_in_ssios": False,
        "note": "Letta Code can execute a shell and stays outside the trusted core.",
    }
    if execute and path:
        env = os.environ.copy()
        env["LETTA_CODE_TELEM"] = "0"
        env["DISABLE_AUTOUPDATER"] = "1"
        completed = subprocess.run(
            [path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
            env=env,
        )
        result["executed"] = True
        result["returncode"] = completed.returncode
        result["stdout"] = completed.stdout[:200]
    return result
