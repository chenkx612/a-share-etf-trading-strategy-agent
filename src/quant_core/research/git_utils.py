"""Git primitives used by isolated research workspaces."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path


GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Quant Research Harness",
    "GIT_AUTHOR_EMAIL": "quant-research@example.invalid",
    "GIT_COMMITTER_NAME": "Quant Research Harness",
    "GIT_COMMITTER_EMAIL": "quant-research@example.invalid",
}


def git(
    root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={**os.environ, **(env or {})},
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def git_bytes(
    root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={**os.environ, **(env or {})},
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()
        if not detail:
            detail = completed.stdout.decode(errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


@contextmanager
def temporary_git_write_environment(
    root: Path,
    *,
    prefix: str,
) -> Iterator[dict[str, str]]:
    """Keep temporary index and object writes outside the source repository."""
    common_dir = Path(git(root, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = (root / common_dir).resolve()
    source_objects = common_dir / "objects"
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
        temporary_root = Path(temporary)
        object_directory = temporary_root / "objects"
        object_directory.mkdir()
        alternates = json.dumps(str(source_objects), ensure_ascii=False)
        inherited_alternates = os.environ.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
        if inherited_alternates:
            alternates = os.pathsep.join((alternates, inherited_alternates))
        yield {
            "GIT_INDEX_FILE": str(temporary_root / "index"),
            "GIT_OBJECT_DIRECTORY": str(object_directory),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": alternates,
        }
