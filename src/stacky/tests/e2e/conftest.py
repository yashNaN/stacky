"""Pytest fixtures for stacky end-to-end tests.

Each test gets a fresh toy git repo under tmp_path and a fake `gh` on PATH.
Both are cleaned up automatically by pytest's tmp_path teardown.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from stacky.tests.e2e.helpers import ToyRepo

_FAKE_GH_SRC = Path(__file__).parent / "fake_gh" / "gh"


@pytest.fixture
def fake_gh_dir(tmp_path_factory) -> Path:
    """Directory containing an executable `gh` stub."""
    d = tmp_path_factory.mktemp("fake_gh")
    dst = d / "gh"
    shutil.copy(_FAKE_GH_SRC, dst)
    dst.chmod(0o755)
    return d


def _init_repo(path: Path) -> None:
    """Initialize a git repo with a master branch and a seed commit."""
    # Some git versions don't support `git init -b`; fall back to renaming HEAD.
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/master"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path, check=True, capture_output=True,
    )
    # commit.gpgsign off in case the user's global git config turns it on.
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path, check=True, capture_output=True,
    )
    (path / "README").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=path, check=True, capture_output=True,
    )


@pytest.fixture(params=["rebase", "merge"], ids=["rebase", "merge"])
def stacky_mode(request) -> str:
    """Parametrize tests across use_merge=False (rebase) and use_merge=True."""
    return request.param


@pytest.fixture
def toy_repo(tmp_path, fake_gh_dir, stacky_mode) -> ToyRepo:
    """A throwaway git repo with master @ seed commit, stacky-ready.

    Runs twice per test via stacky_mode: once with use_merge=False (rebase),
    once with use_merge=True. The fixture writes the appropriate
    .stackyconfig and exposes the mode via ToyRepo.use_merge so tests can
    skip themselves when needed (e.g. amend is rebase-only).
    """
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_repo(repo_path)
    use_merge = stacky_mode == "merge"
    config = "[UI]\nskip_confirm = True\n"
    if use_merge:
        config += "[GIT]\nuse_merge = True\n"
    (repo_path / ".stackyconfig").write_text(config)
    return ToyRepo(path=repo_path, gh_dir=fake_gh_dir, use_merge=use_merge)


@pytest.fixture
def toy_repo_no_gh(tmp_path) -> ToyRepo:
    """Like toy_repo but without a fake `gh` on PATH — for negative tests."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_repo(repo_path)
    (repo_path / ".stackyconfig").write_text("[UI]\nskip_confirm = True\n")
    return ToyRepo(path=repo_path, gh_dir=None)
