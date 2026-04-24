"""End-to-end tests for stacky workflows.

Each test uses a fresh toy_repo fixture (real git, no remote, fake `gh` on
PATH) and drives stacky via a real subprocess. Assertions read stacky's
on-disk state directly (git refs, branch config) so failures are easy to
trace back to the production code being verified.
"""
from __future__ import annotations

from stacky.tests.e2e.helpers import (
    current_branch,
    head,
    list_branches,
    merge_config,
    stack_parent_ref,
)


def test_branch_new_records_parent(toy_repo):
    """`stacky branch new` creates a branch and records its parent commit ref."""
    master_head = head(toy_repo, "master")

    result = toy_repo.run_stacky("branch", "new", "feature1", check=True)
    assert result.returncode == 0, result.stderr

    assert "feature1" in list_branches(toy_repo)
    assert current_branch(toy_repo) == "feature1"
    # branch.<name>.merge points at parent — mirror of get_stack_parent_branch.
    assert merge_config(toy_repo, "feature1") == "refs/heads/master"
    # refs/stack-parent/<name> captures the parent commit at creation time.
    assert stack_parent_ref(toy_repo, "feature1") == master_head


def test_commit_advances_branch_head(toy_repo):
    """`stacky commit` advances the feature branch but leaves master alone."""
    toy_repo.run_stacky("branch", "new", "feature1", check=True)
    master_head = head(toy_repo, "master")
    feature_head_before = head(toy_repo, "feature1")

    toy_repo.add_file("new_file", "content\n")
    toy_repo.run_stacky("commit", "-m", "add new_file", check=True)

    assert head(toy_repo, "master") == master_head
    assert head(toy_repo, "feature1") != feature_head_before


def test_two_level_stack_info_shows_both_branches(toy_repo):
    """Build master -> f1 -> f2, then `stacky info` mentions both branches."""
    toy_repo.run_stacky("branch", "new", "f1", check=True)
    toy_repo.add_file("a", "a\n")
    toy_repo.run_stacky("commit", "-m", "f1 commit", check=True)

    toy_repo.run_stacky("branch", "new", "f2", check=True)
    toy_repo.add_file("b", "b\n")
    toy_repo.run_stacky("commit", "-m", "f2 commit", check=True)

    result = toy_repo.run_stacky("info", check=True)
    assert "f1" in result.stdout
    assert "f2" in result.stdout
    assert "master" in result.stdout

    # Parent wiring for the upper branch.
    assert merge_config(toy_repo, "f2") == "refs/heads/f1"


def test_sync_rebases_upstack_on_parent_change(toy_repo):
    """Committing on A auto-syncs (rebases) B on top of A's new head.

    Exercises do_sync in src/stacky/stack/operations.py:131.
    """
    toy_repo.run_stacky("branch", "new", "A", check=True)
    toy_repo.add_file("a", "a\n")
    toy_repo.run_stacky("commit", "-m", "A1", check=True)

    toy_repo.run_stacky("branch", "new", "B", check=True)
    toy_repo.add_file("b", "b\n")
    toy_repo.run_stacky("commit", "-m", "B1", check=True)

    # Go back to A, add another commit. This should trigger B's rebase.
    toy_repo.git("checkout", "A")
    toy_repo.add_file("a2", "a2\n")
    toy_repo.run_stacky("commit", "-m", "A2", check=True)

    new_a_head = head(toy_repo, "A")
    # After the auto-sync, B's recorded parent commit should equal A's new head.
    assert stack_parent_ref(toy_repo, "B") == new_a_head
    # And B should still contain its own change on top.
    log = toy_repo.git("log", "--format=%s", f"{new_a_head}..B")
    assert "B1" in log


def test_fold_collapses_branch_into_parent(toy_repo):
    """`stacky fold` on B (master -> A -> B) removes B and moves its commits onto A."""
    toy_repo.run_stacky("branch", "new", "A", check=True)
    toy_repo.add_file("a", "a\n")
    toy_repo.run_stacky("commit", "-m", "A commit", check=True)

    toy_repo.run_stacky("branch", "new", "B", check=True)
    toy_repo.add_file("b", "b\n")
    toy_repo.run_stacky("commit", "-m", "B commit", check=True)

    result = toy_repo.run_stacky("fold", check=True)
    assert result.returncode == 0, result.stderr

    # B is gone; A has B's file.
    assert "B" not in list_branches(toy_repo)
    assert "A" in list_branches(toy_repo)
    # A's tree now contains the "b" file.
    a_files = toy_repo.git("ls-tree", "--name-only", "A").split("\n")
    assert "a" in a_files
    assert "b" in a_files
    # stack-parent ref for B is cleaned up.
    assert stack_parent_ref(toy_repo, "B") is None


def test_adopt_untracked_branch(toy_repo):
    """`stacky adopt` wires a plain git branch into the current stack."""
    # Make a sidebar branch the "dumb" way and go back to master.
    toy_repo.git("checkout", "-b", "sidebar")
    toy_repo.write_file("side", "side\n")
    toy_repo.commit_all("side commit")
    toy_repo.git("checkout", "master")

    master_head = head(toy_repo, "master")
    result = toy_repo.run_stacky("adopt", "sidebar", check=True)
    assert result.returncode == 0, result.stderr

    # adopt records parent=master (merge config) and parent_commit=merge-base.
    assert merge_config(toy_repo, "sidebar") == "refs/heads/master"
    # sidebar was branched from master's initial commit, so merge-base == master_head.
    assert stack_parent_ref(toy_repo, "sidebar") == master_head


def test_init_git_fails_without_gh(toy_repo_no_gh):
    """Negative: with no fake `gh` on PATH, init_git() dies with an auth error.

    Verifies src/stacky/git/branch.py:98 is actually being exercised.
    """
    result = toy_repo_no_gh.run_stacky("info")
    assert result.returncode != 0
    assert "gh" in (result.stdout + result.stderr).lower()
