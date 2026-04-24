"""Helpers for stacky end-to-end tests.

The ToyRepo handle wraps a throwaway git repo and runs real stacky commands
against it as subprocesses. State inspection goes through plain git commands,
which mirror stacky's own internal reads (see src/stacky/git/refs.py and
src/stacky/git/branch.py).
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


@dataclasses.dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


@dataclasses.dataclass
class ToyRepo:
    path: Path
    gh_dir: Optional[Path]  # None = strip any real `gh` from PATH for negative tests.
    use_merge: bool = False  # Reflects the .stackyconfig written by the fixture.

    def _env(self) -> dict:
        env = dict(os.environ)
        # Isolate HOME so ~/.stacky.state and ~/.stackyconfig don't leak real state.
        env["HOME"] = str(self.path.parent)
        if self.gh_dir is not None:
            env["PATH"] = f"{self.gh_dir}{os.pathsep}{env.get('PATH', '')}"
        else:
            # Drop any directory containing a `gh` executable so init_git()'s
            # `gh auth status` check reliably fails even if the host has gh installed.
            env["PATH"] = _path_without_gh(env.get("PATH", ""))
        # Turn off any git pager / prompts for determinism.
        env["GIT_PAGER"] = "cat"
        env["GIT_TERMINAL_PROMPT"] = "0"
        # Stacky uses this for color decisions; force off so stdout is plain text.
        env["NO_COLOR"] = "1"
        return env

    def run_stacky(self, *args: str, check: bool = False) -> RunResult:
        """Invoke `python -m stacky <args>` in the toy repo."""
        sp = subprocess.run(
            [sys.executable, "-m", "stacky", *args],
            cwd=self.path,
            env=self._env(),
            capture_output=True,
            text=True,
        )
        result = RunResult(sp.returncode, sp.stdout, sp.stderr)
        if check and sp.returncode != 0:
            raise AssertionError(
                f"stacky {' '.join(args)} failed ({sp.returncode}).\n"
                f"stdout:\n{sp.stdout}\nstderr:\n{sp.stderr}"
            )
        return result

    def git(self, *args: str, check: bool = True) -> str:
        """Run a git command in the toy repo, returning stripped stdout."""
        sp = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env=self._env(),
            capture_output=True,
            text=True,
        )
        if check and sp.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed ({sp.returncode}).\n"
                f"stdout:\n{sp.stdout}\nstderr:\n{sp.stderr}"
            )
        return sp.stdout.strip()

    def write_file(self, name: str, contents: str = "") -> None:
        (self.path / name).write_text(contents)

    def add_file(self, name: str, contents: str = "") -> None:
        """Write a file and `git add` it — needed before `stacky commit`,
        since stacky's `-a` flag (just `git commit -a`) doesn't pick up
        untracked files.
        """
        self.write_file(name, contents)
        self.git("add", name)

    def commit_all(self, message: str) -> None:
        """Plain `git add -A && git commit -m ...` — bypasses stacky."""
        self.git("add", "-A")
        self.git("commit", "-m", message)


def _path_without_gh(path: str) -> str:
    """Return PATH with any directory that contains a `gh` executable removed."""
    kept = []
    for entry in path.split(os.pathsep):
        if not entry:
            continue
        gh = os.path.join(entry, "gh")
        if os.path.isfile(gh) and os.access(gh, os.X_OK):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


def stack_parent_ref(repo: ToyRepo, branch: str) -> Optional[str]:
    """Read refs/stack-parent/<branch>, returning None if missing.

    Mirrors stacky.git.refs.get_stack_parent_commit (src/stacky/git/refs.py:10).
    """
    sp = subprocess.run(
        ["git", "rev-parse", f"refs/stack-parent/{branch}"],
        cwd=repo.path,
        env=repo._env(),
        capture_output=True,
        text=True,
    )
    if sp.returncode != 0:
        return None
    return sp.stdout.strip()


def merge_config(repo: ToyRepo, branch: str) -> Optional[str]:
    """Read branch.<branch>.merge, returning None if unset.

    Mirrors stacky.git.branch.get_stack_parent_branch (src/stacky/git/branch.py:61).
    """
    sp = subprocess.run(
        ["git", "config", f"branch.{branch}.merge"],
        cwd=repo.path,
        env=repo._env(),
        capture_output=True,
        text=True,
    )
    if sp.returncode != 0:
        return None
    return sp.stdout.strip()


def head(repo: ToyRepo, branch: str) -> str:
    """Resolve refs/heads/<branch> to its commit sha."""
    return repo.git("rev-parse", f"refs/heads/{branch}")


def list_branches(repo: ToyRepo) -> List[str]:
    """List local branches (short names)."""
    out = repo.git("for-each-ref", "--format=%(refname:short)", "refs/heads")
    return [line for line in out.split("\n") if line]


def current_branch(repo: ToyRepo) -> str:
    return repo.git("symbolic-ref", "--short", "HEAD")
