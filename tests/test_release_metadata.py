from __future__ import annotations

from pathlib import Path
import sys
import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import threadlang  # noqa: E402


def test_package_versions_match() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as pyproject:
        metadata = tomllib.load(pyproject)
    assert metadata["project"]["version"] == threadlang.__version__
