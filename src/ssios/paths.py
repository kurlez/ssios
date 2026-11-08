"""Repository paths."""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    override = os.environ.get("SSIOS_ROOT")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[2]


def fixtures_dir() -> Path:
    return repo_root() / "fixtures"
