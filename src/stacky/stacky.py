#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK

# GitHub helper for stacked diffs.
#
# Git maintains all metadata locally. Does everything by forking "git" and "gh"
# commands.
#
# Theory of operation:
#
# Each entry in a stack is a branch, set to track its parent (that is, `git
# config branch.<name>.remote` is ".", and `git config branch.<name>.merge` is
# "refs/heads/<parent>")
#
# For each branch, we maintain a ref (call it PC, for "parent commit") pointing
# to the commit at the tip of the parent branch, as `git update-ref
# refs/stack-parent/<name>`.
#
# For all bottom branches we maintain a ref, labeling it a bottom_branch refs/stacky-bottom-branch/branch-name
#
# When rebasing or restacking, we proceed in depth-first order (from "master"
# onwards). After updating a parent branch P, given a child branch C,
# we rebase everything from C's PC until C's tip onto P.
#
#
# That's all there is to it.

import configparser
import dataclasses
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from argparse import ArgumentParser
from typing import Dict, FrozenSet, Generator, List, NewType, Optional, Tuple, TypedDict, Union

import argcomplete  # type: ignore
import asciitree  # type: ignore
import colors  # type: ignore
from simple_term_menu import TerminalMenu  # type: ignore

BranchName = NewType("BranchName", str)
PathName = NewType("PathName", str)
Commit = NewType("Commit", str)
CmdArgs = NewType("CmdArgs", List[str])
StackSubTree = Tuple["StackBranch", "BranchesTree"]
TreeNode = Tuple[BranchName, StackSubTree]
BranchesTree = NewType("BranchesTree", Dict[BranchName, StackSubTree])
BranchesTreeForest = NewType("BranchesTreeForest", List[BranchesTree])

JSON = Union[Dict[str, "JSON"], List["JSON"], str, int, float, bool, None]


class PRInfo(TypedDict):
    id: str
    number: int
    state: str
    mergeable: str
    url: str
    title: str
    baseRefName: str
    headRefName: str
    commits: List[Dict[str, str]]


@dataclasses.dataclass
class PRInfos:
    all: Dict[str, PRInfo]
    open: Optional[PRInfo]


@dataclasses.dataclass
class BranchNCommit:
    branch: BranchName
    parent_commit: Optional[str]


_LOGGING_FORMAT = "%(asctime)s %(module)s %(levelname)s: %(message)s"

# 2 minutes ought to be enough for anybody ;-)
MAX_SSH_MUX_LIFETIME = 120
COLOR_STDOUT: bool = os.isatty(1)
COLOR_STDERR: bool = os.isatty(2)
IS_TERMINAL: bool = os.isatty(1) and os.isatty(2)
CURRENT_BRANCH: BranchName
STACK_BOTTOMS: set[BranchName] = set([BranchName("master"), BranchName("main")])
FROZEN_STACK_BOTTOMS: FrozenSet[BranchName] = frozenset([BranchName("master"), BranchName("main")])
STATE_FILE = os.path.expanduser("~/.stacky.state")
TMP_STATE_FILE = STATE_FILE + ".tmp"

LOGLEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


@dataclasses.dataclass
class StackyConfig:
    skip_confirm: bool = False
    change_to_main: bool = False
    change_to_adopted: bool = False
    share_ssh_session: bool = False
    use_merge: bool = False
    use_force_push: bool = True

    def read_one_config(self, config_path: str):
        rawconfig = configparser.ConfigParser()
        rawconfig.read(config_path)
        if rawconfig.has_section("UI"):
            self.skip_confirm = bool(rawconfig.get("UI", "skip_confirm", fallback=self.skip_confirm))
            self.change_to_main = bool(rawconfig.get("UI", "change_to_main", fallback=self.change_to_main))
            self.change_to_adopted = bool(rawconfig.get("UI", "change_to_adopted", fallback=self.change_to_adopted))
            self.share_ssh_session = bool(rawconfig.get("UI", "share_ssh_session", fallback=self.share_ssh_session))

        if rawconfig.has_section("GIT"):
            self.use_merge = bool(rawconfig.get("GIT", "use_merge", fallback=self.use_merge))
            self.use_force_push = bool(rawconfig.get("GIT", "use_force_push", fallback=self.use_force_push))


CONFIG: Optional[StackyConfig] = None


def get_config() -> StackyConfig:
    global CONFIG
    if CONFIG is None:
        CONFIG = read_config()
    return CONFIG


def read_config() -> StackyConfig:
    config = StackyConfig()
    config_paths = [os.path.expanduser("~/.stackyconfig")]

    try:
        root_dir = get_top_level_dir()
        config_paths.append(f"{root_dir}/.stackyconfig")
    except Exception:
        # Not in a git repository, skip the repo-level config
        debug("Not in a git repository, skipping repo-level config")
        pass

    for p in config_paths:
        # Root dir config overwrites home directory config
        if os.path.exists(p):
            config.read_one_config(p)

    return config


def fmt(s: str, *args, color: bool = False, fg=None, bg=None, style=None, **kwargs) -> str:
    s = colors.color(s, fg=fg, bg=bg, style=style) if color else s
    return s.format(*args, **kwargs)


def cout(*args, **kwargs):
    return sys.stdout.write(fmt(*args, color=COLOR_STDOUT, **kwargs))


def _log(fn, *args, **kwargs):
    return fn("%s", fmt(*args, color=COLOR_STDERR, **kwargs))


def debug(*args, **kwargs):
    return _log(logging.debug, *args, fg="green", **kwargs)


def info(*args, **kwargs):
    return _log(logging.info, *args, fg="green", **kwargs)


def warning(*args, **kwargs):
    return _log(logging.warning, *args, fg="yellow", **kwargs)


def error(*args, **kwargs):
    return _log(logging.error, *args, fg="red", **kwargs)


class ExitException(BaseException):
    def __init__(self, fmt, *args, **kwargs):
        super().__init__(fmt.format(*args, **kwargs))


def stop_muxed_ssh(remote: str = "origin"):
    if get_config().share_ssh_session:
        hostish = get_remote_type(remote)
        if hostish is not None:
            cmd = gen_ssh_mux_cmd()
            cmd.append("-O")
            cmd.append("exit")
            cmd.append(hostish)
            subprocess.Popen(cmd, stderr=subprocess.DEVNULL)


def die(*args, **kwargs):
    # We are taking a wild guess at what is the remote ...
    # TODO (mpatou) fix the assumption about the remote
    stop_muxed_ssh()
    raise ExitException(*args, **kwargs)


def _check_returncode(sp: subprocess.CompletedProcess, cmd: CmdArgs):
    rc = sp.returncode
    if rc == 0:
        return
    stderr = sp.stderr.decode("UTF-8")
    if rc < 0:
        die("Killed by signal {}: {}. Stderr was:\n{}", -rc, shlex.join(cmd), stderr)
    else:
        die("Exited with status {}: {}. Stderr was:\n{}", rc, shlex.join(cmd), stderr)


def run_multiline(cmd: CmdArgs, *, check: bool = True, null: bool = True, out: bool = False) -> Optional[str]:
    debug("Running: {}", shlex.join(cmd))
    sys.stdout.flush()
    sys.stderr.flush()
    sp = subprocess.run(
        cmd,
        stdout=1 if out else subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check:
        _check_returncode(sp, cmd)
    rc = sp.returncode
    if rc != 0:
        return None
    if sp.stdout is None:
        return ""
    return sp.stdout.decode("UTF-8")


def run_always_return(cmd: CmdArgs, **kwargs) -> str:
    out = run(cmd, **kwargs)
    assert out is not None
    return out


def run(cmd: CmdArgs, **kwargs) -> Optional[str]:
    out = run_multiline(cmd, **kwargs)
    return None if out is None else out.strip()


def remove_prefix(s: str, prefix: str) -> str:
    if not s.startswith(prefix):
        die('Invalid string "{}": expected prefix "{}"', s, prefix)
    return s[len(prefix) :]  # noqa: E203


def get_current_branch() -> Optional[BranchName]:
    s = run(CmdArgs(["git", "symbolic-ref", "-q", "HEAD"]))
    if s is not None:
        return BranchName(remove_prefix(s, "refs/heads/"))
    return None


def get_all_branches() -> List[BranchName]:
    branches = run_multiline(CmdArgs(["git", "for-each-ref", "--format", "%(refname:short)", "refs/heads"]))
    assert branches is not None
    return [BranchName(b) for b in branches.split("\n") if b]


def branch_name_completer(prefix, parsed_args, **kwargs):
    """Argcomplete completer function for branch names."""
    try:
        branches = get_all_branches()
        return [branch for branch in branches if branch.startswith(prefix)]
    except Exception:
        return []


def get_real_stack_bottom() -> Optional[BranchName]:  # type: ignore [return]
    """
    return the actual stack bottom for this current repo
    """
    branches = get_all_branches()
    candiates = set()
    for b in branches:
        if b in STACK_BOTTOMS:
            candiates.add(b)

    if len(candiates) == 1:
        return candiates.pop()


def get_stack_parent_branch(branch: BranchName) -> Optional[BranchName]:  # type: ignore [return]
    if branch in STACK_BOTTOMS:
        return None
    p = run(CmdArgs(["git", "config", "branch.{}.merge".format(branch)]), check=False)
    if p is not None:
        p = remove_prefix(p, "refs/heads/")
        if BranchName(p) == branch:
            return None
        return BranchName(p)


def get_top_level_dir() -> PathName:
    p = run_always_return(CmdArgs(["git", "rev-parse", "--show-toplevel"]))
    return PathName(p)


def get_stack_parent_commit(branch: BranchName) -> Optional[Commit]:  # type: ignore [return]
    c = run(
        CmdArgs(["git", "rev-parse", "refs/stack-parent/{}".format(branch)]),
        check=False,
    )

    if c is not None:
        return Commit(c)


def get_commit(branch: BranchName) -> Commit:  # type: ignore [return]
    c = run_always_return(CmdArgs(["git", "rev-parse", "refs/heads/{}".format(branch)]), check=False)
    return Commit(c)


def get_pr_info(branch: BranchName, *, full: bool = False) -> PRInfos:
    fields = [
        "id",
        "number",
        "state",
        "mergeable",
        "url",
        "title",
        "baseRefName",
        "headRefName",
    ]
    if full:
        fields += ["commits"]
    data = json.loads(
        run_always_return(
            CmdArgs(
                [
                    "gh",
                    "pr",
                    "list",
                    "--json",
                    ",".join(fields),
                    "--state",
                    "all",
                    "--head",
                    branch,
                ]
            )
        )
    )
    raw_infos: List[PRInfo] = data

    infos: Dict[str, PRInfo] = {info["id"]: info for info in raw_infos}
    open_prs: List[PRInfo] = [info for info in infos.values() if info["state"] == "OPEN"]
    if len(open_prs) > 1:
        die(
            "Branch {} has more than one open PR: {}",
            branch,
            ", ".join([str(pr) for pr in open_prs]),
        )  # type: ignore[arg-type]
    return PRInfos(infos, open_prs[0] if open_prs else None)


# (remote, remote_branch, remote_branch_commit)
def get_remote_info(branch: BranchName) -> Tuple[str, BranchName, Optional[Commit]]:
    if branch not in STACK_BOTTOMS:
        remote = run(CmdArgs(["git", "config", "branch.{}.remote".format(branch)]), check=False)
        if remote != ".":
            die("Misconfigured branch {}: remote {}", branch, remote)

    # TODO(tudor): Maybe add a way to change these.
    remote = "origin"
    remote_branch = branch

    remote_commit = run(
        CmdArgs(["git", "rev-parse", "refs/remotes/{}/{}".format(remote, remote_branch)]),
        check=False,
    )

    # TODO(mpatou): do something when remote_commit is none
    commit = None
    if remote_commit is not None:
        commit = Commit(remote_commit)

    return (remote, BranchName(remote_branch), commit)


class StackBranch:
    def __init__(
        self,
        name: BranchName,
        parent: "StackBranch",
        parent_commit: Commit,
    ):
        self.name = name
        self.parent = parent
        self.parent_commit = parent_commit
        self.children: set["StackBranch"] = set()
        self.commit = get_commit(name)
        self.remote, self.remote_branch, self.remote_commit = get_remote_info(name)
        self.pr_info: Dict[str, PRInfo] = {}
        self.open_pr_info: Optional[PRInfo] = None
        self._pr_info_loaded = False

    def is_synced_with_parent(self):
        return self.parent is None or self.parent_commit == self.parent.commit

    def is_synced_with_remote(self):
        return self.commit == self.remote_commit

    def __repr__(self):
        return f"StackBranch: {self.name} {len(self.children)} {self.commit}"

    def load_pr_info(self):
        if not self._pr_info_loaded:
            self._pr_info_loaded = True
            pr_infos = get_pr_info(self.name)
            # FIXME maybe store the whole object and use it elsewhere
            self.pr_info, self.open_pr_info = (
                pr_infos.all,
                pr_infos.open,
            )


class StackBranchSet:
    def __init__(self: "StackBranchSet"):
        self.stack: Dict[BranchName, StackBranch] = {}
        self.tops: set[StackBranch] = set()
        self.bottoms: set[StackBranch] = set()

    def add(self, name: BranchName, **kwargs) -> StackBranch:
        if name in self.stack:
            s = self.stack[name]
            assert s.name == name
            for k, v in kwargs.items():
                if getattr(s, k) != v:
                    die(
                        "Mismatched stack: {}: {}={}, expected {}",
                        name,
                        k,
                        getattr(s, k),
                        v,
                    )
        else:
            s = StackBranch(name, **kwargs)
            self.stack[name] = s
            if s.parent is None:
                self.bottoms.add(s)
            self.tops.add(s)
        return s

    def addStackBranch(self, s: StackBranch):
        if s.name not in self.stack:
            self.stack[s.name] = s
            if s.parent is None:
                self.bottoms.add(s)
            if len(s.children) == 0:
                self.tops.add(s)

        return s

    def remove(self, name: BranchName) -> Optional[StackBranch]:
        if name in self.stack:
            s = self.stack[name]
            assert s.name == name
            del self.stack[name]
            if s in self.tops:
                self.tops.remove(s)
            if s in self.bottoms:
                self.bottoms.remove(s)
            return s

        return None

    def __repr__(self) -> str:
        out = f"StackBranchSet: {self.stack}"
        return out

    def add_child(self, s: StackBranch, child: StackBranch):
        s.children.add(child)
        self.tops.discard(s)


def load_stack_for_given_branch(
    stack: StackBranchSet, branch: BranchName, *, check: bool = True
) -> Tuple[Optional[StackBranch], List[BranchName]]:
    """Given a stack of branch and a branch name,
    update the stack with all the parents of the specified branch
    if the branch is part of an existing stack.
    Return also a list of BranchName of all the branch bellow the specified one
    """
    branches: List[BranchNCommit] = []
    while branch not in STACK_BOTTOMS:
        parent = get_stack_parent_branch(branch)
        parent_commit = get_stack_parent_commit(branch)
        branches.append(BranchNCommit(branch, parent_commit))
        if not parent or not parent_commit:
            if check:
                die("Branch is not in a stack: {}", branch)
            return None, [b.branch for b in branches]
        branch = parent

    branches.append(BranchNCommit(branch, None))
    top = None
    for b in reversed(branches):
        n = stack.add(
            b.branch,
            parent=top,
            parent_commit=b.parent_commit,
        )
        if top:
            stack.add_child(top, n)
        top = n

    return top, [b.branch for b in branches]


def get_branch_name_from_short_ref(ref: str) -> BranchName:
    parts = ref.split("/", 1)
    if len(parts) != 2:
        die("invalid ref: {}".format(ref))

    return BranchName(parts[1])


def get_all_stack_bottoms() -> List[BranchName]:
    branches = run_multiline(
        CmdArgs(["git", "for-each-ref", "--format", "%(refname:short)", "refs/stacky-bottom-branch"])
    )
    if branches:
        return [get_branch_name_from_short_ref(b) for b in branches.split("\n") if b]
    return []


def get_all_stack_parent_refs() -> List[BranchName]:
    branches = run_multiline(CmdArgs(["git", "for-each-ref", "--format", "%(refname:short)", "refs/stack-parent"]))
    if branches:
        return [get_branch_name_from_short_ref(b) for b in branches.split("\n") if b]
    return []


def load_all_stack_bottoms():
    branches = run_multiline(
        CmdArgs(["git", "for-each-ref", "--format", "%(refname:short)", "refs/stacky-bottom-branch"])
    )
    STACK_BOTTOMS.update(get_all_stack_bottoms())


def load_all_stacks(stack: StackBranchSet) -> Optional[StackBranch]:
    """Given a stack return the top of it, aka the bottom of the tree"""
    load_all_stack_bottoms()
    all_branches = set(get_all_branches())
    current_branch_top = None
    while all_branches:
        b = all_branches.pop()
        top, branches = load_stack_for_given_branch(stack, b, check=False)
        all_branches -= set(branches)
        if top is None:
            if len(branches) > 1:
                # Incomplete (broken) stack
                warning("Broken stack: {}", " -> ".join(branches))
            continue
        if b == CURRENT_BRANCH:
            current_branch_top = top
    return current_branch_top


def make_tree_node(b: StackBranch) -> TreeNode:
    return (b.name, (b, make_subtree(b)))


def make_subtree(b) -> BranchesTree:
    return BranchesTree(dict(make_tree_node(c) for c in sorted(b.children, key=lambda x: x.name)))


def make_tree(b: StackBranch) -> BranchesTree:
    return BranchesTree(dict([make_tree_node(b)]))


def format_name(b: StackBranch, *, colorize: bool) -> str:
    prefix = ""
    severity = 0
    # TODO: Align things so that we have the same prefix length ?
    if not b.is_synced_with_parent():
        prefix += fmt("!", color=colorize, fg="yellow")
        severity = max(severity, 2)
    if not b.is_synced_with_remote():
        prefix += fmt("~", color=colorize, fg="yellow")
    if b.name == CURRENT_BRANCH:
        prefix += fmt("*", color=colorize, fg="cyan")
    else:
        severity = max(severity, 1)
    if prefix:
        prefix += " "
    fg = ["cyan", "green", "yellow", "red"][severity]
    suffix = ""
    if b.open_pr_info:
        suffix += " "
        suffix += fmt("(#{})", b.open_pr_info["number"], color=colorize, fg="blue")
        suffix += " "
        suffix += fmt("{}", b.open_pr_info["title"], color=colorize, fg="blue")
    return prefix + fmt("{}", b.name, color=colorize, fg=fg) + suffix


def format_tree(tree: BranchesTree, *, colorize: bool = False):
    return {
        format_name(branch, colorize=colorize): format_tree(children, colorize=colorize)
        for branch, children in tree.values()
    }


# Print upside down, to match our "upstack" / "downstack" nomenclature
_ASCII_TREE_BOX = {
    "UP_AND_RIGHT": "\u250c",
    "HORIZONTAL": "\u2500",
    "VERTICAL": "\u2502",
    "VERTICAL_AND_RIGHT": "\u251c",
}
_ASCII_TREE_STYLE = asciitree.drawing.BoxStyle(gfx=_ASCII_TREE_BOX)
ASCII_TREE = asciitree.LeftAligned(draw=_ASCII_TREE_STYLE)


def print_tree(tree: BranchesTree):
    global ASCII_TREE
    s = ASCII_TREE(format_tree(tree, colorize=COLOR_STDOUT))
    lines = s.split("\n")
    print("\n".join(reversed(lines)))


def print_forest(trees: List[BranchesTree]):
    for i, t in enumerate(trees):
        if i != 0:
            print()
        print_tree(t)


def get_all_stacks_as_forest(stack: StackBranchSet) -> BranchesTreeForest:
    return BranchesTreeForest([make_tree(b) for b in stack.bottoms])


def get_current_stack_as_forest(stack: StackBranchSet):
    b = stack.stack[CURRENT_BRANCH]
    d: BranchesTree = make_tree(b)
    b = b.parent
    while b:
        d = BranchesTree({b.name: (b, d)})
        b = b.parent
    return [d]


def get_current_upstack_as_forest(stack: StackBranchSet) -> BranchesTreeForest:
    b = stack.stack[CURRENT_BRANCH]
    return BranchesTreeForest([make_tree(b)])


def get_current_downstack_as_forest(stack: StackBranchSet) -> BranchesTreeForest:
    b = stack.stack[CURRENT_BRANCH]
    d: BranchesTree = BranchesTree({})
    while b:
        d = BranchesTree({b.name: (b, d)})
        b = b.parent
    return BranchesTreeForest([d])


def init_git():
    push_default = run(["git", "config", "remote.pushDefault"], check=False)
    if push_default is not None:
        die("`git config remote.pushDefault` may not be set")
    auth_status = run(["gh", "auth", "status"], check=False)
    if auth_status is None:
        die("`gh` authentication failed")
    global CURRENT_BRANCH
    CURRENT_BRANCH = get_current_branch()


def forest_depth_first(
    forest: BranchesTreeForest,
) -> Generator[StackBranch, None, None]:
    for tree in forest:
        for b in depth_first(tree):
            yield b


def depth_first(tree: BranchesTree) -> Generator[StackBranch, None, None]:
    # This is for the regular forest
    for _, (branch, children) in tree.items():
        yield branch
        for b in depth_first(children):
            yield b


def menu_choose_branch(forest: BranchesTreeForest):
    if not IS_TERMINAL:
        die("May only choose from menu when using a terminal")

    global ASCII_TREE
    s = ""
    lines = []
    for tree in forest:
        s = ASCII_TREE(format_tree(tree))
        lines += [l.rstrip() for l in s.split("\n")]
    lines.reverse()

    initial_index = 0
    for i, l in enumerate(lines):
        if "*" in l:  # lol
            initial_index = i
            break

    menu = TerminalMenu(lines, cursor_index=initial_index)
    idx = menu.show()
    if idx is None:
        die("Aborted")

    branches = list(forest_depth_first(forest))
    branches.reverse()
    return branches[idx]


def load_pr_info_for_forest(forest: BranchesTreeForest):
    for b in forest_depth_first(forest):
        b.load_pr_info()


def cmd_info(stack: StackBranchSet, args):
    forest = get_all_stacks_as_forest(stack)
    if args.pr:
        load_pr_info_for_forest(forest)
    print_forest(forest)


def cmd_log(stack: StackBranchSet, args):
    config = get_config()
    if config.use_merge:
        run(["git", "log", "--no-merges", "--first-parent"], out=True)
    else:
        run(["git", "log"], out=True)


def checkout(branch):
    info("Checking out branch {}", branch)
    run(["git", "checkout", branch], out=True)


def cmd_branch_up(stack: StackBranchSet, args):
    b = stack.stack[CURRENT_BRANCH]
    if not b.children:
        info("Branch {} is already at the top of the stack", CURRENT_BRANCH)
        return
    if len(b.children) > 1:
        if not IS_TERMINAL:
            die(
                "Branch {} has multiple children: {}",
                CURRENT_BRANCH,
                ", ".join(c.name for c in b.children),
            )
        cout(
            "Branch {} has {} children, choose one\n",
            CURRENT_BRANCH,
            len(b.children),
            fg="green",
        )
        forest = BranchesTreeForest([BranchesTree({BranchName(c.name): (c, BranchesTree({}))}) for c in b.children])
        child = menu_choose_branch(forest).name
    else:
        child = next(iter(b.children)).name
    checkout(child)


def cmd_branch_down(stack: StackBranchSet, args):
    b = stack.stack[CURRENT_BRANCH]
    if not b.parent:
        info("Branch {} is already at the bottom of the stack", CURRENT_BRANCH)
        return
    checkout(b.parent.name)


def create_branch(branch):
    run(["git", "checkout", "-b", branch, "--track"], out=True)


def cmd_branch_new(stack: StackBranchSet, args):
    b = stack.stack[CURRENT_BRANCH]
    assert b.commit
    name = args.name
    create_branch(name)
    run(CmdArgs(["git", "update-ref", "refs/stack-parent/{}".format(name), b.commit, ""]))


def cmd_branch_commit(stack: StackBranchSet, args):
    """Create a new branch and commit all changes with the provided message"""
    global CURRENT_BRANCH

    # First create the new branch (same logic as cmd_branch_new)
    b = stack.stack[CURRENT_BRANCH]
    assert b.commit
    name = args.name
    create_branch(name)
    run(CmdArgs(["git", "update-ref", "refs/stack-parent/{}".format(name), b.commit, ""]))

    # Update global CURRENT_BRANCH since we just checked out the new branch
    CURRENT_BRANCH = BranchName(name)

    # Reload the stack to include the new branch
    load_stack_for_given_branch(stack, CURRENT_BRANCH)

    # Now commit all changes with the provided message (or open editor if no message)
    do_commit(
        stack,
        message=args.message,
        amend=False,
        allow_empty=False,
        edit=True,
        add_all=args.add_all,
        no_verify=args.no_verify,
    )


def cmd_branch_checkout(stack: StackBranchSet, args):
    branch_name = args.name
    if branch_name is None:
        forest = get_all_stacks_as_forest(stack)
        branch_name = menu_choose_branch(forest).name
    checkout(branch_name)


def cmd_stack_info(stack: StackBranchSet, args):
    forest = get_current_stack_as_forest(stack)
    if args.pr:
        load_pr_info_for_forest(forest)
    print_forest(forest)


def cmd_stack_checkout(stack: StackBranchSet, args):
    forest = get_current_stack_as_forest(stack)
    branch_name = menu_choose_branch(forest).name
    checkout(branch_name)


def prompt(message: str, default_value: Optional[str]) -> str:
    cout(message)
    if default_value is not None:
        cout("({})", default_value, fg="gray")
        cout(" ")
    while True:
        sys.stderr.flush()
        r = input().strip()

        if len(r) > 0:
            return r
        if default_value:
            return default_value


def confirm(msg: str = "Proceed?"):
    if get_config().skip_confirm:
        return
    if not os.isatty(0):
        die("Standard input is not a terminal, use --force option to force action")
    print()
    while True:
        cout("{} [yes/no] ", msg, fg="yellow")
        sys.stderr.flush()
        r = input().strip().lower()
        if r == "yes" or r == "y":
            break
        if r == "no":
            die("Not confirmed")
        cout("Please answer yes or no\n", fg="red")


def find_reviewers(b: StackBranch) -> Optional[List[str]]:
    out = run_multiline(
        CmdArgs(
            [
                "git",
                "log",
                "--pretty=format:%b",
                "-1",
                f"{b.name}",
            ]
        ),
    )
    assert out is not None
    for l in out.split("\n"):
        reviewer_match = re.match(r"^reviewers?\s*:\s*(.*)", l, re.I)
        if reviewer_match:
            reviewers = reviewer_match.group(1).split(",")
            logging.debug(f"Found the following reviewers: {', '.join(reviewers)}")
            return reviewers
    return None


def find_issue_marker(name: str) -> Optional[str]:
    match = re.search(r"(?:^|[_-])([A-Z]{3,}[_-]?\d{2,})($|[_-].*)", name)
    if match:
        res = match.group(1)
        if "_" in res:
            return res.replace("_", "-")
        if not "-" in res:
            newmatch = re.match(r"(...)(\d+)", res)
            assert newmatch is not None
            return f"{newmatch.group(1)}-{newmatch.group(2)}"
        return res

    return None


def create_gh_pr(b: StackBranch, prefix: str):
    cout("Creating PR for {}\n", b.name, fg="green")
    parent_prefix = ""
    if b.parent.name not in STACK_BOTTOMS:
        # you are pushing a sub stack, there is no way we can make it work
        # accross repo so we will push within your own clone
        prefix = ""
    cmd = [
        "gh",
        "pr",
        "create",
        "--head",
        f"{prefix}{b.name}",
        "--base",
        f"{parent_prefix}{b.parent.name}",
    ]
    reviewers = find_reviewers(b)
    issue_id = find_issue_marker(b.name)
    if issue_id:
        out = run_multiline(
            CmdArgs(["git", "log", "--pretty=oneline", f"{b.parent.name}..{b.name}"]),
        )
        title = f"[{issue_id}] "
        # Just one line (hence 2 elements with the last one being an empty string when we
        # split on "\"n ?
        # Then use the title of the commit as the title of the PR
        if out is not None and len(out.split("\n")) == 2:
            out = run(
                CmdArgs(
                    [
                        "git",
                        "log",
                        "--pretty=format:%s",
                        "-1",
                        f"{b.name}",
                    ]
                ),
                out=False,
            )
            if out is None:
                out = ""
            if b.name not in out:
                title += out
            else:
                title = out

        title = prompt(
            (fmt("? ", color=COLOR_STDOUT, fg="green") + fmt("Title ", color=COLOR_STDOUT, style="bold", fg="white")),
            title,
        )
        cmd.extend(["--title", title.strip()])
    if reviewers:
        logging.debug(f"Adding {len(reviewers)} reviewer(s) to the review")
        for r in reviewers:
            r = r.strip()
            r = r.replace("#", "rockset/")
            if len(r) > 0:
                cmd.extend(["--reviewer", r])

    run(
        CmdArgs(cmd),
        out=True,
    )


def generate_stack_string(forest: BranchesTreeForest, current_branch: StackBranch) -> str:
    """Generate a string representation of the PR stack"""
    stack_lines = []

    def add_branch_to_stack(b: StackBranch, depth: int):
        if b.name in STACK_BOTTOMS:
            return

        indent = "  " * depth
        pr_info = ""
        if b.open_pr_info:
            pr_info = f" (#{b.open_pr_info['number']})"
        
        # Add arrow indicator for current branch (the one this PR represents)
        current_indicator = " ← (CURRENT PR)" if b.name == current_branch.name else ""
        
        stack_lines.append(f"{indent}- {b.name}{pr_info}{current_indicator}")
    
    def traverse_tree(tree: BranchesTree, depth: int):
        for _, (branch, children) in tree.items():
            add_branch_to_stack(branch, depth)
            traverse_tree(children, depth + 1)

    for tree in forest:
        traverse_tree(tree, 0)

    if not stack_lines:
        return ""

    return "\n".join([
        "<!-- Stacky Stack Info -->",
        "**Stack:**",
        *stack_lines,
        "<!-- End Stacky Stack Info -->"
    ])


def get_branch_depth(branch: StackBranch, forest: BranchesTreeForest) -> int:
    """Calculate the depth of a branch in the stack"""
    depth = 0
    b = branch
    while b.parent and b.parent.name not in STACK_BOTTOMS:
        depth += 1
        b = b.parent
    return depth


def extract_stack_comment(body: str) -> str:
    """Extract existing stack comment from PR body"""
    if not body:
        return ""

    # Look for the stack comment pattern using HTML comments as sentinels
    import re
    pattern = r'<!-- Stacky Stack Info -->.*?<!-- End Stacky Stack Info -->'
    match = re.search(pattern, body, re.DOTALL)

    if match:
        return match.group(0).strip()
    return ""


def add_or_update_stack_comment(branch: StackBranch, forest: BranchesTreeForest):
    """Add or update stack comment in PR body"""
    if not branch.open_pr_info:
        return

    pr_number = branch.open_pr_info["number"]

    # Get current PR body
    pr_data = json.loads(
        run_always_return(
            CmdArgs([
                "gh", "pr", "view", str(pr_number),
                "--json", "body"
            ])
        )
    )

    current_body = pr_data.get("body", "")
    stack_string = generate_stack_string(forest, branch)
    
    if not stack_string:
        return

    existing_stack = extract_stack_comment(current_body)

    if not existing_stack:
        # No existing stack comment, add one
        if current_body:
            new_body = f"{current_body}\n\n{stack_string}"
        else:
            new_body = stack_string

        cout("Adding stack comment to PR #{}\n", pr_number, fg="green")
        run(CmdArgs([
            "gh", "pr", "edit", str(pr_number),
            "--body", new_body
        ]), out=True)
    else:
        # Verify existing stack comment is correct
        if existing_stack != stack_string:
            # Update the stack comment
            updated_body = current_body.replace(existing_stack, stack_string)

            cout("Updating stack comment in PR #{}\n", pr_number, fg="yellow")
            run(CmdArgs([
                "gh", "pr", "edit", str(pr_number),
                "--body", updated_body
            ]), out=True)
        else:
            cout("✓ Stack comment in PR #{} is already correct\n", pr_number, fg="green")


def do_push(
    forest: BranchesTreeForest,
    *,
    force: bool = False,
    pr: bool = False,
    remote_name: str = "origin",
):
    if pr:
        load_pr_info_for_forest(forest)
    print_forest(forest)
    for b in forest_depth_first(forest):
        if not b.is_synced_with_parent():
            die(
                "Branch {} is not synced with parent {}, sync first",
                b.name,
                b.parent.name,
            )

    # (branch, push, pr_action)
    PR_NONE = 0
    PR_FIX_BASE = 1
    PR_CREATE = 2
    actions = []
    for b in forest_depth_first(forest):
        if not b.parent:
            cout("✓ Not pushing base branch {}\n", b.name, fg="green")
            continue

        push = False
        if b.is_synced_with_remote():
            cout(
                "✓ Not pushing branch {}, synced with remote {}/{}\n",
                b.name,
                b.remote,
                b.remote_branch,
                fg="green",
            )
        else:
            cout("- Will push branch {} to {}/{}\n", b.name, b.remote, b.remote_branch)
            push = True

        pr_action = PR_NONE
        if pr:
            if b.open_pr_info:
                expected_base = b.parent.name
                if b.open_pr_info["baseRefName"] != expected_base:
                    cout(
                        "- Branch {} already has open PR #{}; will change PR base from {} to {}\n",
                        b.name,
                        b.open_pr_info["number"],
                        b.open_pr_info["baseRefName"],
                        expected_base,
                    )
                    pr_action = PR_FIX_BASE
                else:
                    cout(
                        "✓ Branch {} already has open PR #{}\n",
                        b.name,
                        b.open_pr_info["number"],
                        fg="green",
                    )
            else:
                cout("- Will create PR for branch {}\n", b.name)
                pr_action = PR_CREATE

        if not push and pr_action == PR_NONE:
            continue

        actions.append((b, push, pr_action))

    if actions and not force:
        confirm()

    # Figure out if we need to add a prefix to the branch
    # ie. user:foo
    # We should call gh repo set-default before doing that
    val = run(CmdArgs(["git", "config", f"remote.{remote_name}.gh-resolved"]), check=False)
    if val is not None and "/" in val:
        # If there is a "/" in the gh-resolved it means that the repo where
        # the should be created is not the same as the one where the push will
        # be made, we need to add a prefix to the branch in the gh pr command
        val = run_always_return(CmdArgs(["git", "config", f"remote.{remote_name}.url"]))
        prefix = f'{val.split(":")[1].split("/")[0]}:'
    else:
        prefix = ""
    muxed = False
    for b, push, pr_action in actions:
        if push:
            if not muxed:
                start_muxed_ssh(remote_name)
                muxed = True
            # Try to run pre-push before muxing ...
            # To do so we need to pickup the current commit of the branch, the branch name, the
            # parent branch and it's parent commit and call .git/hooks/pre-push
            cout("Pushing {}\n", b.name, fg="green")
            run(
                CmdArgs(
                    [
                        "git",
                        "push",
                        "-f" if get_config().use_force_push else "",
                        b.remote,
                        "{}:{}".format(b.name, b.remote_branch),
                    ]
                ),
                out=True,
            )
        if pr_action == PR_FIX_BASE:
            cout("Fixing PR base for {}\n", b.name, fg="green")
            assert b.open_pr_info is not None
            run(
                CmdArgs(
                    [
                        "gh",
                        "pr",
                        "edit",
                        str(b.open_pr_info["number"]),
                        "--base",
                        b.parent.name,
                    ]
                ),
                out=True,
            )
        elif pr_action == PR_CREATE:
            create_gh_pr(b, prefix)

    # Handle stack comments for PRs
    if pr:
        for b in forest_depth_first(forest):
            if b.open_pr_info:
                add_or_update_stack_comment(b, forest)

    stop_muxed_ssh(remote_name)


def cmd_stack_push(stack: StackBranchSet, args):
    do_push(
        get_current_stack_as_forest(stack),
        force=args.force,
        pr=args.pr,
        remote_name=args.remote_name,
    )


def do_sync(forest: BranchesTreeForest):
    print_forest(forest)

    syncs: List[StackBranch] = []
    sync_names: List[BranchName] = []
    syncs_set: set[StackBranch] = set()
    for b in forest_depth_first(forest):
        if not b.parent:
            cout("✓ Not syncing base branch {}\n", b.name, fg="green")
            continue
        if b.is_synced_with_parent() and not b.parent in syncs_set:
            cout(
                "✓ Not syncing branch {}, already synced with parent {}\n",
                b.name,
                b.parent.name,
                fg="green",
            )
            continue
        syncs.append(b)
        syncs_set.add(b)
        sync_names.append(b.name)
        cout("- Will sync branch {} on top of {}\n", b.name, b.parent.name)

    if not syncs:
        return

    syncs.reverse()
    sync_names.reverse()
    # TODO: use list(syncs_set).reverse() ?
    inner_do_sync(syncs, sync_names)


def set_parent_commit(branch: BranchName, new_commit: Commit, prev_commit: Optional[str] = None):
    cmd = [
        "git",
        "update-ref",
        "refs/stack-parent/{}".format(branch),
        new_commit,
    ]
    if prev_commit is not None:
        cmd.append(prev_commit)
    run(CmdArgs(cmd))


def get_commits_between(a: Commit, b: Commit):
    lines = run_multiline(CmdArgs(["git", "rev-list", "{}..{}".format(a, b)]))
    assert lines is not None
    # Have to strip the last element because it's empty, rev list includes a new line at the end it seems
    return [x.strip() for x in lines.split("\n")][:-1]

def inner_do_sync(syncs: List[StackBranch], sync_names: List[BranchName]):
    print()
    sync_type = "merge" if get_config().use_merge else "rebase"
    while syncs:
        with open(TMP_STATE_FILE, "w") as f:
            json.dump({"branch": CURRENT_BRANCH, "sync": sync_names}, f)
        os.replace(TMP_STATE_FILE, STATE_FILE)  # make the write atomic

        b = syncs.pop()
        sync_names.pop()
        if b.is_synced_with_parent():
            cout("{} is already synced on top of {}\n", b.name, b.parent.name)
            continue
        if b.parent.commit in get_commits_between(b.parent_commit, b.commit):
            cout(
                "Recording complete {} of {} on top of {}\n",
                sync_type,
                b.name,
                b.parent.name,
                fg="green",
            )
        else:
            r = None
            if get_config().use_merge:
                cout("Merging {} into {}\n", b.parent.name, b.name, fg="green")
                run(CmdArgs(["git", "checkout", str(b.name)]))
                r = run(
                    CmdArgs(["git", "merge", b.parent.name]),
                    out=True,
                    check=False,
                )
            else:
                cout("Rebasing {} on top of {}\n", b.name, b.parent.name, fg="green")
                r = run(
                    CmdArgs(["git", "rebase", "--onto", b.parent.name, b.parent_commit, b.name]),
                    out=True,
                    check=False,
                )

            if r is None:
                print()
                die(
                    "Automatic {0} failed. Please complete the {0} (fix conflicts; `git {0} --continue`), then run `stacky continue`".format(
                        sync_type
                    )
                )
            b.commit = get_commit(b.name)
        set_parent_commit(b.name, b.parent.commit, b.parent_commit)
        b.parent_commit = b.parent.commit
    run(CmdArgs(["git", "checkout", str(CURRENT_BRANCH)]))


def cmd_stack_sync(stack: StackBranchSet, args):
    do_sync(get_current_stack_as_forest(stack))


def do_commit(stack: StackBranchSet, *, message=None, amend=False, allow_empty=False, edit=True, add_all=False, no_verify=False):
    b = stack.stack[CURRENT_BRANCH]
    if not b.parent:
        die("Do not commit directly on {}", b.name)
    if not b.is_synced_with_parent():
        die(
            "Branch {} is not synced with parent {}, sync before committing",
            b.name,
            b.parent.name,
        )

    if amend and (get_config().use_merge or not get_config().use_force_push):
        die("Amending is not allowed if using git merge or if force pushing is disallowed")

    if amend and b.commit == b.parent.commit:
        die("Branch {} has no commits, may not amend", b.name)

    cmd = ["git", "commit"]
    if add_all:
        cmd += ["-a"]
    if allow_empty:
        cmd += ["--allow-empty"]
    if no_verify:
        cmd += ["--no-verify"]
    if amend:
        cmd += ["--amend"]
        if not edit:
            cmd += ["--no-edit"]
    elif not edit:
        die("--no-edit is only supported with --amend")
    if message:
        cmd += ["-m", message]
    run(CmdArgs(cmd), out=True)

    # Sync everything upstack
    b.commit = get_commit(b.name)
    do_sync(get_current_upstack_as_forest(stack))


def cmd_commit(stack: StackBranchSet, args):
    do_commit(
        stack,
        message=args.message,
        amend=args.amend,
        allow_empty=args.allow_empty,
        edit=not args.no_edit,
        add_all=args.add_all,
        no_verify=args.no_verify,
    )


def cmd_amend(stack: StackBranchSet, args):
    do_commit(stack, amend=True, edit=False, no_verify=args.no_verify)


def cmd_upstack_info(stack: StackBranchSet, args):
    forest = get_current_upstack_as_forest(stack)
    if args.pr:
        load_pr_info_for_forest(forest)
    print_forest(forest)


def cmd_upstack_push(stack: StackBranchSet, args):
    do_push(
        get_current_upstack_as_forest(stack),
        force=args.force,
        pr=args.pr,
        remote_name=args.remote_name,
    )


def cmd_upstack_sync(stack: StackBranchSet, args):
    do_sync(get_current_upstack_as_forest(stack))


def set_parent(branch: BranchName, target: Optional[BranchName], *, set_origin: bool = False):
    if set_origin:
        run(CmdArgs(["git", "config", "branch.{}.remote".format(branch), "."]))

    ## If target is none this becomes a new stack bottom
    run(
        CmdArgs(
            [
                "git",
                "config",
                "branch.{}.merge".format(branch),
                "refs/heads/{}".format(target if target is not None else branch),
            ]
        )
    )

    if target is None:
        run(
            CmdArgs(
                [
                    "git",
                    "update-ref",
                    "-d",
                    "refs/stack-parent/{}".format(branch),
                ]
            )
        )


def cmd_upstack_onto(stack: StackBranchSet, args):
    b = stack.stack[CURRENT_BRANCH]
    if not b.parent:
        die("may not upstack a stack bottom, use stacky adopt")
    target = stack.stack[args.target]
    upstack = get_current_upstack_as_forest(stack)
    for ub in forest_depth_first(upstack):
        if ub == target:
            die("Target branch {} is upstack of {}", target.name, b.name)
    b.parent = target
    set_parent(b.name, target.name)

    do_sync(upstack)


def cmd_upstack_as_base(stack: StackBranchSet):
    b = stack.stack[CURRENT_BRANCH]
    if not b.parent:
        die("Branch {} is already a stack bottom", b.name)

    b.parent = None  # type: ignore
    stack.remove(b.name)
    stack.addStackBranch(b)
    set_parent(b.name, None)

    run(CmdArgs(["git", "update-ref", "refs/stacky-bottom-branch/{}".format(b.name), b.commit, ""]))
    info("Set {} as new bottom branch".format(b.name))


def cmd_upstack_as(stack: StackBranchSet, args):
    if args.target == "bottom":
        cmd_upstack_as_base(stack)
    else:
        die("Invalid target {}, acceptable targets are [base]", args.target)


def cmd_downstack_info(stack, args):
    forest = get_current_downstack_as_forest(stack)
    if args.pr:
        load_pr_info_for_forest(forest)
    print_forest(forest)


def cmd_downstack_push(stack: StackBranchSet, args):
    do_push(
        get_current_downstack_as_forest(stack),
        force=args.force,
        pr=args.pr,
        remote_name=args.remote_name,
    )


def cmd_downstack_sync(stack: StackBranchSet, args):
    do_sync(get_current_downstack_as_forest(stack))


def get_bottom_level_branches_as_forest(stack: StackBranchSet) -> BranchesTreeForest:
    return BranchesTreeForest(
        [
            BranchesTree(
                {
                    bottom.name: (
                        bottom,
                        BranchesTree({b.name: (b, BranchesTree({})) for b in bottom.children}),
                    )
                }
            )
            for bottom in stack.bottoms
        ]
    )


def get_remote_type(remote: str = "origin") -> Optional[str]:
    out = run_always_return(CmdArgs(["git", "remote", "-v"]))
    for l in out.split("\n"):
        match = re.match(r"^{}\s+(?:ssh://)?([^/]*):(?!//).*\s+\(push\)$".format(remote), l)
        if match:
            sshish_host = match.group(1)
            return sshish_host

    return None


def gen_ssh_mux_cmd() -> List[str]:
    args = [
        "ssh",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPersist={MAX_SSH_MUX_LIFETIME}",
        "-o",
        "ControlPath=~/.ssh/stacky-%C",
    ]

    return args


def start_muxed_ssh(remote: str = "origin"):
    if not get_config().share_ssh_session:
        return
    hostish = get_remote_type(remote)
    if hostish is not None:
        info("Creating a muxed ssh connection")
        cmd = gen_ssh_mux_cmd()
        os.environ["GIT_SSH_COMMAND"] = " ".join(cmd)
        cmd.append("-MNf")
        cmd.append(hostish)
        # We don't want to use the run() wrapper because
        # we don't want to wait for the process to finish

        p = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        # Wait a little bit for the connection to establish
        # before carrying on
        while p.poll() is None:
            time.sleep(1)
        if p.returncode != 0:
            if p.stderr is not None:
                error = p.stderr.read()
            else:
                error = b"unknown"
            die(f"Failed to start ssh muxed connection, error was: {error.decode('utf-8').strip()}")


def get_branches_to_delete(forest: BranchesTreeForest) -> List[StackBranch]:
    deletes = []
    for b in forest_depth_first(forest):
        if not b.parent or b.open_pr_info:
            continue
        for pr_info in b.pr_info.values():
            if pr_info["state"] != "MERGED":
                continue
            cout(
                "- Will delete branch {}, PR #{} merged into {}\n",
                b.name,
                pr_info["number"],
                b.parent.name,
            )
            deletes.append(b)
            for c in b.children:
                cout(
                    "- Will reparent branch {} onto {}\n",
                    c.name,
                    b.parent.name,
                )
            break
    return deletes


def delete_branches(stack: StackBranchSet, deletes: List[StackBranch]):
    global CURRENT_BRANCH
    # Make sure we're not trying to delete the current branch
    for b in deletes:
        for c in b.children:
            info("Reparenting {} onto {}", c.name, b.parent.name)
            c.parent = b.parent
            set_parent(c.name, b.parent.name)
        info("Deleting {}", b.name)
        if b.name == CURRENT_BRANCH:
            new_branch = next(iter(stack.bottoms))
            info("About to delete current branch, switching to {}", new_branch.name)
            run(CmdArgs(["git", "checkout", new_branch.name]))
            CURRENT_BRANCH = new_branch.name
        run(CmdArgs(["git", "branch", "-D", b.name]))


def cleanup_unused_refs(stack: StackBranchSet):
    # Clean up stacky bottom branch refs
    info("Cleaning up unused refs")

    # Get the current list of existing branches in the repository
    existing_branches = set(get_all_branches())

    # Clean up stacky bottom branch refs for non-existent branches
    stack_bottoms = get_all_stack_bottoms()
    for bottom in stack_bottoms:
        if bottom not in stack.stack or bottom not in existing_branches:
            ref = "refs/stacky-bottom-branch/{}".format(bottom)
            info("Deleting ref {} (branch {} no longer exists)".format(ref, bottom))
            run(CmdArgs(["git", "update-ref", "-d", ref]))

    # Clean up stack parent refs for non-existent branches
    stack_parent_refs = get_all_stack_parent_refs()
    for br in stack_parent_refs:
        if br not in stack.stack or br not in existing_branches:
            ref = "refs/stack-parent/{}".format(br)
            old_value = run(CmdArgs(["git", "show-ref", ref]), check=False)
            if old_value:
                info("Deleting ref {} (branch {} no longer exists)".format(old_value, br))
            else:
                info("Deleting ref refs/stack-parent/{} (branch {} no longer exists)".format(br, br))
            run(CmdArgs(["git", "update-ref", "-d", ref]))


def cmd_update(stack: StackBranchSet, args):
    remote = "origin"
    start_muxed_ssh(remote)
    info("Fetching from {}", remote)
    run(CmdArgs(["git", "fetch", remote]))

    # TODO(tudor): We should rebase instead of silently dropping
    # everything you have on local master. Oh well.
    global CURRENT_BRANCH
    for b in stack.bottoms:
        run(
            CmdArgs(
                [
                    "git",
                    "update-ref",
                    "refs/heads/{}".format(b.name),
                    "refs/remotes/{}/{}".format(remote, b.remote_branch),
                ]
            )
        )
        if b.name == CURRENT_BRANCH:
            run(CmdArgs(["git", "reset", "--hard", "HEAD"]))

    # We treat origin as the source of truth for bottom branches (master), and
    # the local repo as the source of truth for everything else. So we can only
    # track PR closure for branches that are direct descendants of master.

    info("Checking if any PRs have been merged and can be deleted")
    forest = get_bottom_level_branches_as_forest(stack)
    load_pr_info_for_forest(forest)

    deletes = get_branches_to_delete(forest)
    if deletes and not args.force:
        confirm()

    delete_branches(stack, deletes)
    stop_muxed_ssh(remote)

    info("Cleaning up refs for non-existent branches")
    cleanup_unused_refs(stack)


def cmd_import(stack: StackBranchSet, args):
    # Importing has to happen based on PR info, rather than local branch
    # relationships, as that's the only place Graphite populates.
    branch = args.name
    branches = []
    bottoms = set(b.name for b in stack.bottoms)
    while branch not in bottoms:
        pr_info = get_pr_info(branch, full=True)
        open_pr = pr_info.open
        info("Getting PR information for {}", branch)
        if open_pr is None:
            die("Branch {} has no open PR", branch)
            # Never reached because the die but makes mypy happy
            assert open_pr is not None
        if open_pr["headRefName"] != branch:
            die(
                "Branch {} is misconfigured: PR #{} head is {}",
                branch,
                open_pr["number"],
                open_pr["headRefName"],
            )
        if not open_pr["commits"]:
            die("PR #{} has no commits", open_pr["number"])
        first_commit = open_pr["commits"][0]["oid"]
        parent_commit = Commit(run_always_return(CmdArgs(["git", "rev-parse", "{}^".format(first_commit)])))
        next_branch = open_pr["baseRefName"]
        info(
            "Branch {}: PR #{}, parent is {} at commit {}",
            branch,
            open_pr["number"],
            next_branch,
            parent_commit,
        )
        branches.append((branch, parent_commit))
        branch = next_branch

    if not branches:
        return

    base_branch = branch
    branches.reverse()

    for b, parent_commit in branches:
        cout(
            "- Will set parent of {} to {} at commit {}\n",
            b,
            branch,
            parent_commit,
        )
        branch = b

    if not args.force:
        confirm()

    branch = base_branch
    for b, parent_commit in branches:
        set_parent(b, branch, set_origin=True)
        set_parent_commit(b, parent_commit)
        branch = b


def get_merge_base(b1: BranchName, b2: BranchName):
    return run(CmdArgs(["git", "merge-base", str(b1), str(b2)]))


def cmd_adopt(stack: StackBranch, args):
    """
    Adopt a branch that is based on the current branch (which must be a
    valid stack bottom or the stack bottom (master or main) will be used
    if change_to_main option is set in the config file
    """
    branch = args.name
    global CURRENT_BRANCH

    if branch == CURRENT_BRANCH:
        die("A branch cannot adopt itself")

    if CURRENT_BRANCH not in STACK_BOTTOMS:
        # TODO remove that, the initialisation code is already dealing with that in fact
        main_branch = get_real_stack_bottom()

        if get_config().change_to_main and main_branch is not None:
            run(CmdArgs(["git", "checkout", main_branch]))
            CURRENT_BRANCH = main_branch
        else:
            die(
                "The current branch {} must be a valid stack bottom: {}",
                CURRENT_BRANCH,
                ", ".join(sorted(STACK_BOTTOMS)),
            )
    if branch in STACK_BOTTOMS:
        if branch in FROZEN_STACK_BOTTOMS:
            die("Cannot adopt frozen stack bottoms {}".format(FROZEN_STACK_BOTTOMS))
        # Remove the ref that this is a stack bottom
        run(CmdArgs(["git", "update-ref", "-d", "refs/stacky-bottom-branch/{}".format(branch)]))

    parent_commit = get_merge_base(CURRENT_BRANCH, branch)
    set_parent(branch, CURRENT_BRANCH, set_origin=True)
    set_parent_commit(branch, parent_commit)
    if get_config().change_to_adopted:
        run(CmdArgs(["git", "checkout", branch]))


def cmd_land(stack: StackBranchSet, args):
    forest = get_current_downstack_as_forest(stack)
    assert len(forest) == 1
    branches = []
    p = forest[0]
    while p:
        assert len(p) == 1
        _, (b, p) = next(iter(p.items()))
        branches.append(b)
    assert branches
    assert branches[0] in stack.bottoms
    if len(branches) == 1:
        die("May not land {}", branches[0].name)

    b = branches[1]
    if not b.is_synced_with_parent():
        die(
            "Branch {} is not synced with parent {}, sync before landing",
            b.name,
            b.parent.name,
        )
    if not b.is_synced_with_remote():
        die(
            "Branch {} is not synced with remote branch, push local changes before landing",
            b.name,
        )

    b.load_pr_info()
    pr = b.open_pr_info
    if not pr:
        die("Branch {} does not have an open PR", b.name)
        assert pr is not None

    if pr["mergeable"] != "MERGEABLE":
        die(
            "PR #{} for branch {} is not mergeable: {}",
            pr["number"],
            b.name,
            pr["mergeable"],
        )

    if len(branches) > 2:
        cout(
            "The `land` command only lands the bottom-most branch {}; the current stack has {} branches, ending with {}\n",
            b.name,
            len(branches) - 1,
            CURRENT_BRANCH,
            fg="yellow",
        )

    msg = fmt("- Will land PR #{} (", pr["number"], color=COLOR_STDOUT)
    msg += fmt("{}", pr["url"], color=COLOR_STDOUT, fg="blue")
    msg += fmt(") for branch {}", b.name, color=COLOR_STDOUT)
    msg += fmt(" into branch {}\n", b.parent.name, color=COLOR_STDOUT)
    sys.stdout.write(msg)

    if not args.force:
        confirm()

    v = run(CmdArgs(["git", "rev-parse", b.name]))
    assert v is not None
    head_commit = Commit(v)
    cmd = CmdArgs(["gh", "pr", "merge", b.name, "--squash", "--match-head-commit", head_commit])
    if args.auto:
        cmd.append("--auto")
    run(cmd, out=True)
    cout("\n✓ Success! Run `stacky update` to update local state.\n", fg="green")


def edit_pr_description(pr):
    """Edit a PR's description using the user's default editor"""
    import tempfile

    cout("Editing PR #{} - {}\n", pr["number"], pr["title"], fg="green")
    cout("Current description:\n", fg="yellow")
    current_body = pr.get("body", "")
    if current_body:
        cout("{}\n\n", current_body, fg="gray")
    else:
        cout("(No description)\n\n", fg="gray")

    # Create a temporary file with the current description
    with tempfile.NamedTemporaryFile(mode='w+', suffix='.md', delete=False) as temp_file:
        temp_file.write(current_body or "")
        temp_file_path = temp_file.name

    try:
        # Get the user's preferred editor
        editor = os.environ.get('EDITOR', 'vim')

        # Open the editor
        result = subprocess.run([editor, temp_file_path])
        if result.returncode != 0:
            cout("Editor exited with error, not updating PR description.\n", fg="red")
            return

        # Read the edited content
        with open(temp_file_path, 'r') as temp_file:
            new_body = temp_file.read().strip()

        # Normalize both original and new content for comparison
        original_content = (current_body or "").strip()
        new_content = new_body.strip()

        # Check if the content actually changed
        if new_content == original_content:
            cout("No changes made to PR description.\n", fg="yellow")
            return

        # Update the PR description using gh CLI
        cout("Updating PR description...\n", fg="green")
        run(CmdArgs([
            "gh", "pr", "edit", str(pr["number"]),
            "--body", new_body
        ]), out=True)

        cout("✓ Successfully updated PR #{} description\n", pr["number"], fg="green")

        # Update the PR object for display consistency
        pr["body"] = new_body

    except Exception as e:
        cout("Error editing PR description: {}\n", str(e), fg="red")
    finally:
        # Clean up the temporary file
        try:
            os.unlink(temp_file_path)
        except OSError:
            pass


def cmd_inbox(stack: StackBranchSet, args):
    """List all active GitHub pull requests for the current user"""
    fields = [
        "number",
        "title",
        "headRefName",
        "baseRefName",
        "state",
        "url",
        "createdAt",
        "updatedAt",
        "author",
        "reviewDecision",
        "reviewRequests",
        "mergeable",
        "mergeStateStatus",
        "statusCheckRollup",
        "isDraft",
        "body"
    ]

    # Get all open PRs authored by the current user
    my_prs_data = json.loads(
        run_always_return(
            CmdArgs(
                [
                    "gh",
                    "pr",
                    "list",
                    "--json",
                    ",".join(fields),
                    "--state",
                    "open",
                    "--author",
                    "@me"
                ]
            )
        )
    )

    # Get all open PRs where current user is requested as reviewer
    review_prs_data = json.loads(
        run_always_return(
            CmdArgs(
                [
                    "gh",
                    "pr",
                    "list",
                    "--json",
                    ",".join(fields),
                    "--state",
                    "open",
                    "--search",
                    "review-requested:@me"
                ]
            )
        )
    )

    # Categorize my PRs based on review status
    waiting_on_me = []
    waiting_on_review = []
    approved = []

    for pr in my_prs_data:
        if pr.get("isDraft", False):
            # Draft PRs are always waiting on the author (me)
            waiting_on_me.append(pr)
        elif pr["reviewDecision"] == "APPROVED":
            approved.append(pr)
        elif pr["reviewRequests"] and len(pr["reviewRequests"]) > 0:
            waiting_on_review.append(pr)
        else:
            # No pending review requests, likely needs changes or author action
            waiting_on_me.append(pr)

    # Sort all lists by updatedAt in descending order (most recent first)
    waiting_on_me.sort(key=lambda pr: pr["updatedAt"], reverse=True)
    waiting_on_review.sort(key=lambda pr: pr["updatedAt"], reverse=True)
    approved.sort(key=lambda pr: pr["updatedAt"], reverse=True)
    review_prs_data.sort(key=lambda pr: pr["updatedAt"], reverse=True)

    def get_check_status(pr):
        """Get a summary of merge check status"""
        if not pr.get("statusCheckRollup") or len(pr.get("statusCheckRollup")) == 0:
            return "", "gray"

        rollup = pr["statusCheckRollup"]

        # statusCheckRollup is a list of checks, determine overall state
        states = []
        for check in rollup:
            if isinstance(check, dict) and "state" in check:
                states.append(check["state"])

        if not states:
            return "", "gray"

        # Determine overall status based on individual check states
        if "FAILURE" in states or "ERROR" in states:
            return "✗ Checks failed", "red"
        elif "PENDING" in states or "QUEUED" in states:
            return "⏳ Checks running", "yellow"
        elif all(state == "SUCCESS" for state in states):
            return "✓ Checks passed", "green"
        else:
            return f"Checks mixed", "yellow"

    def display_pr_compact(pr, show_author=False):
        """Display a single PR in compact format"""
        check_text, check_color = get_check_status(pr)

        # Create clickable link for PR number
        pr_number_text = f"#{pr['number']}"
        clickable_number = f"\033]8;;{pr['url']}\033\\\033[96m{pr_number_text}\033[0m\033]8;;\033\\"
        cout("{} ", clickable_number)
        cout("{} ", pr["title"], fg="white")
        cout("({}) ", pr["headRefName"], fg="gray")

        if show_author:
            cout("by {} ", pr["author"]["login"], fg="gray")

        if pr.get("isDraft", False):
            cout("[DRAFT] ", fg="orange")

        if check_text:
            cout("{} ", check_text, fg=check_color)

        cout("Updated: {}\n", pr["updatedAt"][:10], fg="gray")

    def display_pr_full(pr, show_author=False):
        """Display a single PR in full format"""
        check_text, check_color = get_check_status(pr)

        # Create clickable link for PR number
        pr_number_text = f"#{pr['number']}"
        clickable_number = f"\033]8;;{pr['url']}\033\\\033[96m{pr_number_text}\033[0m\033]8;;\033\\"
        cout("{} ", clickable_number)
        cout("{}\n", pr["title"], fg="white")
        cout("  {} -> {}\n", pr["headRefName"], pr["baseRefName"], fg="gray")

        if show_author:
            cout("  Author: {}\n", pr["author"]["login"], fg="gray")

        if pr.get("isDraft", False):
            cout("  [DRAFT]\n", fg="orange")

        if check_text:
            cout("  {}\n", check_text, fg=check_color)

        cout("  {}\n", pr["url"], fg="blue")
        cout("  Updated: {}, Created: {}\n\n", pr["updatedAt"][:10], pr["createdAt"][:10], fg="gray")

    def display_pr_list(prs, show_author=False):
        """Display a list of PRs in the chosen format"""
        for pr in prs:
            if args.compact:
                display_pr_compact(pr, show_author)
            else:
                display_pr_full(pr, show_author)

    # Display categorized authored PRs
    if waiting_on_me:
        cout("Your PRs - Waiting on You:\n", fg="red")
        display_pr_list(waiting_on_me)
        cout("\n")

    if waiting_on_review:
        cout("Your PRs - Waiting on Review:\n", fg="yellow")
        display_pr_list(waiting_on_review)
        cout("\n")

    if approved:
        cout("Your PRs - Approved:\n", fg="green")
        display_pr_list(approved)
        cout("\n")

    if not my_prs_data:
        cout("No active pull requests authored by you.\n", fg="green")

    # Display PRs waiting for review
    if review_prs_data:
        cout("Pull Requests Awaiting Your Review:\n", fg="yellow")
        display_pr_list(review_prs_data, show_author=True)
    else:
        cout("No pull requests awaiting your review.\n", fg="yellow")


def cmd_prs(stack: StackBranchSet, args):
    """Interactive PR management - select and edit PR descriptions"""
    fields = [
        "number",
        "title",
        "headRefName",
        "baseRefName",
        "state",
        "url",
        "createdAt",
        "updatedAt",
        "author",
        "reviewDecision",
        "reviewRequests",
        "mergeable",
        "mergeStateStatus",
        "statusCheckRollup",
        "isDraft",
        "body"
    ]

    # Get all open PRs authored by the current user
    my_prs_data = json.loads(
        run_always_return(
            CmdArgs(
                [
                    "gh",
                    "pr",
                    "list",
                    "--json",
                    ",".join(fields),
                    "--state",
                    "open",
                    "--author",
                    "@me"
                ]
            )
        )
    )

    # Get all open PRs where current user is requested as reviewer
    review_prs_data = json.loads(
        run_always_return(
            CmdArgs(
                [
                    "gh",
                    "pr",
                    "list",
                    "--json",
                    ",".join(fields),
                    "--state",
                    "open",
                    "--search",
                    "review-requested:@me"
                ]
            )
        )
    )

    # Combine all PRs
    all_prs = my_prs_data + review_prs_data
    if not all_prs:
        cout("No active pull requests found.\n", fg="green")
        return

    if not IS_TERMINAL:
        die("Interactive PR management requires a terminal")

    # Create simple menu options
    menu_options = []
    for pr in all_prs:
        # Simple menu line with just PR number and title
        menu_options.append(f"#{pr['number']} {pr['title']}")

    menu_options.append("Exit")

    while True:
        cout("\nSelect a PR to edit its description:\n", fg="cyan")
        menu = TerminalMenu(menu_options, cursor_index=0)
        idx = menu.show()

        if idx is None or idx == len(menu_options) - 1:  # Exit selected or cancelled
            break

        selected_pr = all_prs[idx]
        edit_pr_description(selected_pr)


def main():
    logging.basicConfig(format=_LOGGING_FORMAT, level=logging.INFO)
    try:
        parser = ArgumentParser(description="Handle git stacks")
        parser.add_argument(
            "--log-level",
            default="info",
            choices=LOGLEVELS.keys(),
            help="Set the log level",
        )
        parser.add_argument(
            "--color",
            default="auto",
            choices=["always", "auto", "never"],
            help="Colorize output and error",
        )
        parser.add_argument(
            "--remote-name",
            "-r",
            default="origin",
            help="name of the git remote where branches will be pushed",
        )

        subparsers = parser.add_subparsers(required=True, dest="command")

        # continue
        continue_parser = subparsers.add_parser("continue", help="Continue previously interrupted command")
        continue_parser.set_defaults(func=None)

        # down
        down_parser = subparsers.add_parser("down", help="Go down in the current stack (towards master/main)")
        down_parser.set_defaults(func=cmd_branch_down)
        # up
        up_parser = subparsers.add_parser("up", help="Go up in the current stack (away master/main)")
        up_parser.set_defaults(func=cmd_branch_up)
        # info
        info_parser = subparsers.add_parser("info", help="Stack info")
        info_parser.add_argument("--pr", action="store_true", help="Get PR info (slow)")
        info_parser.set_defaults(func=cmd_info)

        # log
        log_parser = subparsers.add_parser("log", help="Show git log with conditional merge handling")
        log_parser.set_defaults(func=cmd_log)

        # commit
        commit_parser = subparsers.add_parser("commit", help="Commit")
        commit_parser.add_argument("-m", help="Commit message", dest="message")
        commit_parser.add_argument("--amend", action="store_true", help="Amend last commit")
        commit_parser.add_argument("--allow-empty", action="store_true", help="Allow empty commit")
        commit_parser.add_argument("--no-edit", action="store_true", help="Skip editor")
        commit_parser.add_argument("-a", action="store_true", help="Add all files to commit", dest="add_all")
        commit_parser.add_argument("--no-verify", action="store_true", help="Bypass pre-commit and commit-msg hooks")
        commit_parser.set_defaults(func=cmd_commit)

        # amend
        amend_parser = subparsers.add_parser("amend", help="Shortcut for amending last commit")
        amend_parser.add_argument("--no-verify", action="store_true", help="Bypass pre-commit and commit-msg hooks")
        amend_parser.set_defaults(func=cmd_amend)

        # branch
        branch_parser = subparsers.add_parser("branch", aliases=["b"], help="Operations on branches")
        branch_subparsers = branch_parser.add_subparsers(required=True, dest="branch_command")
        branch_up_parser = branch_subparsers.add_parser("up", aliases=["u"], help="Move upstack")
        branch_up_parser.set_defaults(func=cmd_branch_up)

        branch_down_parser = branch_subparsers.add_parser("down", aliases=["d"], help="Move downstack")
        branch_down_parser.set_defaults(func=cmd_branch_down)

        branch_new_parser = branch_subparsers.add_parser("new", aliases=["create"], help="Create a new branch")
        branch_new_parser.add_argument("name", help="Branch name")
        branch_new_parser.set_defaults(func=cmd_branch_new)

        branch_commit_parser = branch_subparsers.add_parser("commit", help="Create a new branch and commit all changes")
        branch_commit_parser.add_argument("name", help="Branch name")
        branch_commit_parser.add_argument("-m", help="Commit message", dest="message")
        branch_commit_parser.add_argument("-a", action="store_true", help="Add all files to commit", dest="add_all")
        branch_commit_parser.add_argument("--no-verify", action="store_true", help="Bypass pre-commit and commit-msg hooks")
        branch_commit_parser.set_defaults(func=cmd_branch_commit)

        branch_checkout_parser = branch_subparsers.add_parser("checkout", aliases=["co"], help="Checkout a branch")
        branch_checkout_parser.add_argument("name", help="Branch name", nargs="?").completer = branch_name_completer
        branch_checkout_parser.set_defaults(func=cmd_branch_checkout)

        # stack
        stack_parser = subparsers.add_parser("stack", aliases=["s"], help="Operations on the full current stack")
        stack_subparsers = stack_parser.add_subparsers(required=True, dest="stack_command")

        stack_info_parser = stack_subparsers.add_parser("info", aliases=["i"], help="Info for current stack")
        stack_info_parser.add_argument("--pr", action="store_true", help="Get PR info (slow)")
        stack_info_parser.set_defaults(func=cmd_stack_info)

        stack_push_parser = stack_subparsers.add_parser("push", help="Push")
        stack_push_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        stack_push_parser.add_argument("--no-pr", dest="pr", action="store_false", help="Skip Create PRs")
        stack_push_parser.set_defaults(func=cmd_stack_push)

        stack_sync_parser = stack_subparsers.add_parser("sync", help="Sync")
        stack_sync_parser.set_defaults(func=cmd_stack_sync)

        stack_checkout_parser = stack_subparsers.add_parser(
            "checkout", aliases=["co"], help="Checkout a branch in this stack"
        )
        stack_checkout_parser.set_defaults(func=cmd_stack_checkout)

        # upstack
        upstack_parser = subparsers.add_parser("upstack", aliases=["us"], help="Operations on the current upstack")
        upstack_subparsers = upstack_parser.add_subparsers(required=True, dest="upstack_command")

        upstack_info_parser = upstack_subparsers.add_parser("info", aliases=["i"], help="Info for current upstack")
        upstack_info_parser.add_argument("--pr", action="store_true", help="Get PR info (slow)")
        upstack_info_parser.set_defaults(func=cmd_upstack_info)

        upstack_push_parser = upstack_subparsers.add_parser("push", help="Push")
        upstack_push_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        upstack_push_parser.add_argument("--no-pr", dest="pr", action="store_false", help="Skip Create PRs")
        upstack_push_parser.set_defaults(func=cmd_upstack_push)

        upstack_sync_parser = upstack_subparsers.add_parser("sync", help="Sync")
        upstack_sync_parser.set_defaults(func=cmd_upstack_sync)

        upstack_onto_parser = upstack_subparsers.add_parser("onto", aliases=["restack"], help="Restack")
        upstack_onto_parser.add_argument("target", help="New parent")
        upstack_onto_parser.set_defaults(func=cmd_upstack_onto)

        upstack_as_parser = upstack_subparsers.add_parser("as", help="Upstack branch this as a new stack bottom")
        upstack_as_parser.add_argument("target", help="bottom, restack this branch as a new stack bottom").completer = branch_name_completer
        upstack_as_parser.set_defaults(func=cmd_upstack_as)

        # downstack
        downstack_parser = subparsers.add_parser(
            "downstack", aliases=["ds"], help="Operations on the current downstack"
        )
        downstack_subparsers = downstack_parser.add_subparsers(required=True, dest="downstack_command")

        downstack_info_parser = downstack_subparsers.add_parser(
            "info", aliases=["i"], help="Info for current downstack"
        )
        downstack_info_parser.add_argument("--pr", action="store_true", help="Get PR info (slow)")
        downstack_info_parser.set_defaults(func=cmd_downstack_info)

        downstack_push_parser = downstack_subparsers.add_parser("push", help="Push")
        downstack_push_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        downstack_push_parser.add_argument("--no-pr", dest="pr", action="store_false", help="Skip Create PRs")
        downstack_push_parser.set_defaults(func=cmd_downstack_push)

        downstack_sync_parser = downstack_subparsers.add_parser("sync", help="Sync")
        downstack_sync_parser.set_defaults(func=cmd_downstack_sync)

        # update
        update_parser = subparsers.add_parser("update", help="Update repo, all bottom branches must exist in remote")
        update_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        update_parser.set_defaults(func=cmd_update)

        # import
        import_parser = subparsers.add_parser("import", help="Import Graphite stack")
        import_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        import_parser.add_argument("name", help="Foreign stack top").completer = branch_name_completer
        import_parser.set_defaults(func=cmd_import)

        # adopt
        adopt_parser = subparsers.add_parser("adopt", help="Adopt one branch")
        adopt_parser.add_argument("name", help="Branch name").completer = branch_name_completer
        adopt_parser.set_defaults(func=cmd_adopt)

        # land
        land_parser = subparsers.add_parser("land", help="Land bottom-most PR on current stack")
        land_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        land_parser.add_argument(
            "--auto",
            "-a",
            action="store_true",
            help="Automatically merge after all checks pass",
        )
        land_parser.set_defaults(func=cmd_land)

        # shortcuts
        push_parser = subparsers.add_parser("push", help="Alias for downstack push")
        push_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        push_parser.add_argument("--no-pr", dest="pr", action="store_false", help="Skip Create PRs")
        push_parser.set_defaults(func=cmd_downstack_push)

        sync_parser = subparsers.add_parser("sync", help="Alias for stack sync")
        sync_parser.set_defaults(func=cmd_stack_sync)

        checkout_parser = subparsers.add_parser("checkout", aliases=["co"], help="Checkout a branch")
        checkout_parser.add_argument("name", help="Branch name", nargs="?").completer = branch_name_completer
        checkout_parser.set_defaults(func=cmd_branch_checkout)

        checkout_parser = subparsers.add_parser("sco", help="Checkout a branch in this stack")
        checkout_parser.set_defaults(func=cmd_stack_checkout)

        # inbox
        inbox_parser = subparsers.add_parser("inbox", help="List all active GitHub pull requests for the current user")
        inbox_parser.add_argument("--compact", "-c", action="store_true", help="Show compact view")
        inbox_parser.set_defaults(func=cmd_inbox)

        # prs
        prs_parser = subparsers.add_parser("prs", help="Interactive PR management - select and edit PR descriptions")
        prs_parser.set_defaults(func=cmd_prs)

        # fold
        fold_parser = subparsers.add_parser("fold", help="Fold current branch into parent branch and delete current branch")
        fold_parser.add_argument("--allow-empty", action="store_true", help="Allow empty commits during cherry-pick")
        fold_parser.set_defaults(func=cmd_fold)

        argcomplete.autocomplete(parser)
        args = parser.parse_args()
        logging.basicConfig(format=_LOGGING_FORMAT, level=LOGLEVELS[args.log_level], force=True)

        global COLOR_STDERR
        global COLOR_STDOUT
        if args.color == "always":
            COLOR_STDERR = True
            COLOR_STDOUT = True
        elif args.color == "never":
            COLOR_STDERR = False
            COLOR_STDOUT = False

        init_git()

        stack = StackBranchSet()
        load_all_stacks(stack)

        global CURRENT_BRANCH
        if args.command == "continue":
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
            except FileNotFoundError as e:  # noqa: F841
                die("No previous command in progress")
            branch = state["branch"]
            run(["git", "checkout", branch])
            CURRENT_BRANCH = branch
            if CURRENT_BRANCH not in stack.stack:
                die("Current branch {} is not in a stack", CURRENT_BRANCH)

            if "sync" in state:
                # Continue sync operation
                sync_names = state["sync"]
                syncs = [stack.stack[n] for n in sync_names]
                inner_do_sync(syncs, sync_names)
            elif "fold" in state:
                # Continue fold operation
                fold_state = state["fold"]
                inner_do_fold(
                    stack,
                    fold_state["fold_branch"],
                    fold_state["parent_branch"],
                    fold_state["commits"],
                    fold_state["children"],
                    fold_state["allow_empty"]
                )
            elif "merge_fold" in state:
                # Continue merge-based fold operation
                merge_fold_state = state["merge_fold"]
                finish_merge_fold_operation(
                    stack,
                    merge_fold_state["fold_branch"],
                    merge_fold_state["parent_branch"],
                    merge_fold_state["children"]
                )
            else:
                die("Unknown operation in progress")
        else:
            # TODO restore the current branch after changing the branch on some commands for
            # instance `info`
            if CURRENT_BRANCH not in stack.stack:
                main_branch = get_real_stack_bottom()

                if get_config().change_to_main and main_branch is not None:
                    run(["git", "checkout", main_branch])
                    CURRENT_BRANCH = main_branch
                else:
                    die("Current branch {} is not in a stack", CURRENT_BRANCH)

            get_current_stack_as_forest(stack)
            args.func(stack, args)

        # Success, delete the state file
        try:
            os.remove(STATE_FILE)
        except FileNotFoundError:
            pass
    except ExitException as e:
        error("{}", e.args[0])
        sys.exit(1)


def cmd_fold(stack: StackBranchSet, args):
    """Fold current branch into parent branch and delete current branch"""
    global CURRENT_BRANCH

    if CURRENT_BRANCH not in stack.stack:
        die("Current branch {} is not in a stack", CURRENT_BRANCH)

    b = stack.stack[CURRENT_BRANCH]

    if not b.parent:
        die("Cannot fold stack bottom branch {}", CURRENT_BRANCH)

    if b.parent.name in STACK_BOTTOMS:
        die("Cannot fold into stack bottom branch {}", b.parent.name)

    if not b.is_synced_with_parent():
        die(
            "Branch {} is not synced with parent {}, sync before folding",
            b.name,
            b.parent.name,
        )

    # Get commits to be applied
    commits_to_apply = get_commits_between(b.parent_commit, b.commit)
    if not commits_to_apply:
        info("No commits to fold from {} into {}", b.name, b.parent.name)
    else:
        cout("Folding {} commits from {} into {}\n", len(commits_to_apply), b.name, b.parent.name, fg="green")

    # Get children that need to be reparented
    children = list(b.children)
    if children:
        cout("Reparenting {} children to {}\n", len(children), b.parent.name, fg="yellow")
        for child in children:
            cout("  {} -> {}\n", child.name, b.parent.name, fg="gray")

    # Switch to parent branch
    checkout(b.parent.name)
    CURRENT_BRANCH = b.parent.name

    # Choose between merge and cherry-pick based on config
    if get_config().use_merge:
        # Merge approach: merge the child branch into parent
        inner_do_merge_fold(stack, b.name, b.parent.name, [child.name for child in children])
    else:
        # Cherry-pick approach: apply individual commits
        if commits_to_apply:
            # Reverse the list since get_commits_between returns newest first
            commits_to_apply = list(reversed(commits_to_apply))
            # Use inner_do_fold for state management
            inner_do_fold(stack, b.name, b.parent.name, commits_to_apply, [child.name for child in children], args.allow_empty)
        else:
            # No commits to apply, just finish the fold operation
            finish_fold_operation(stack, b.name, b.parent.name, [child.name for child in children])

    return  # Early return since both paths handle completion


def inner_do_merge_fold(stack: StackBranchSet, fold_branch_name: BranchName, parent_branch_name: BranchName,
                        children_names: List[BranchName]):
    """Perform merge-based fold operation with state management"""
    print()

    # Save state for potential continuation
    with open(TMP_STATE_FILE, "w") as f:
        json.dump({
            "branch": CURRENT_BRANCH,
            "merge_fold": {
                "fold_branch": fold_branch_name,
                "parent_branch": parent_branch_name,
                "children": children_names,
            }
        }, f)
    os.replace(TMP_STATE_FILE, STATE_FILE)  # make the write atomic

    cout("Merging {} into {}\n", fold_branch_name, parent_branch_name, fg="green")
    result = run(CmdArgs(["git", "merge", fold_branch_name]), check=False)
    if result is None:
        die("Merge failed for branch {}. Please resolve conflicts and run `stacky continue`", fold_branch_name)

    # Merge successful, complete the fold operation
    finish_merge_fold_operation(stack, fold_branch_name, parent_branch_name, children_names)


def finish_merge_fold_operation(stack: StackBranchSet, fold_branch_name: BranchName,
                                parent_branch_name: BranchName, children_names: List[BranchName]):
    """Complete the merge-based fold operation after merge is successful"""
    global CURRENT_BRANCH

    # Get the updated branches from the stack
    fold_branch = stack.stack.get(fold_branch_name)
    parent_branch = stack.stack[parent_branch_name]

    if not fold_branch:
        # Branch might have been deleted already, just finish up
        cout("✓ Merge fold operation completed\n", fg="green")
        return

    # Update parent branch commit in stack
    parent_branch.commit = get_commit(parent_branch_name)

    # Reparent children
    for child_name in children_names:
        if child_name in stack.stack:
            child = stack.stack[child_name]
            info("Reparenting {} from {} to {}", child.name, fold_branch.name, parent_branch.name)
            child.parent = parent_branch
            parent_branch.children.add(child)
            fold_branch.children.discard(child)
            set_parent(child.name, parent_branch.name)
            # Update the child's parent commit to the new parent's tip
            set_parent_commit(child.name, parent_branch.commit, child.parent_commit)
            child.parent_commit = parent_branch.commit

    # Remove the folded branch from its parent's children
    parent_branch.children.discard(fold_branch)

    # Delete the branch
    info("Deleting branch {}", fold_branch.name)
    run(CmdArgs(["git", "branch", "-D", fold_branch.name]))

    # Clean up stack parent ref
    run(CmdArgs(["git", "update-ref", "-d", "refs/stack-parent/{}".format(fold_branch.name)]))

    # Remove from stack
    stack.remove(fold_branch.name)

    cout("✓ Successfully merged and folded {} into {}\n", fold_branch.name, parent_branch.name, fg="green")


def inner_do_fold(stack: StackBranchSet, fold_branch_name: BranchName, parent_branch_name: BranchName,
                  commits_to_apply: List[str], children_names: List[BranchName], allow_empty: bool):
    """Continue folding operation from saved state"""
    print()

    # If no commits to apply, skip cherry-picking and go straight to cleanup
    if not commits_to_apply:
        finish_fold_operation(stack, fold_branch_name, parent_branch_name, children_names)
        return

    while commits_to_apply:
        with open(TMP_STATE_FILE, "w") as f:
            json.dump({
                "branch": CURRENT_BRANCH,
                "fold": {
                    "fold_branch": fold_branch_name,
                    "parent_branch": parent_branch_name,
                    "commits": commits_to_apply,
                    "children": children_names,
                    "allow_empty": allow_empty
                }
            }, f)
        os.replace(TMP_STATE_FILE, STATE_FILE)  # make the write atomic

        commit = commits_to_apply.pop()

        # Check if this commit would be empty by doing a dry-run cherry-pick
        dry_run_result = run(CmdArgs(["git", "cherry-pick", "--no-commit", commit]), check=False)
        if dry_run_result is not None:
            # Check if there are any changes staged
            has_changes = run(CmdArgs(["git", "diff", "--cached", "--quiet"]), check=False) is None

            # Reset the working directory and index since we only wanted to test
            run(CmdArgs(["git", "reset", "--hard", "HEAD"]))

            if not has_changes:
                cout("Skipping empty commit {}\n", commit[:8], fg="yellow")
                continue
        else:
            # Cherry-pick failed during dry run, reset and try normal cherry-pick
            # This could happen due to conflicts, so we'll let the normal cherry-pick handle it
            run(CmdArgs(["git", "reset", "--hard", "HEAD"]), check=False)

        cout("Cherry-picking commit {}\n", commit[:8], fg="green")
        cherry_pick_cmd = ["git", "cherry-pick"]
        if allow_empty:
            cherry_pick_cmd.append("--allow-empty")
        cherry_pick_cmd.append(commit)
        result = run(CmdArgs(cherry_pick_cmd), check=False)
        if result is None:
            die("Cherry-pick failed for commit {}. Please resolve conflicts and run `stacky continue`", commit)

    # All commits applied successfully, now finish the fold operation
    finish_fold_operation(stack, fold_branch_name, parent_branch_name, children_names)


def finish_fold_operation(stack: StackBranchSet, fold_branch_name: BranchName,
                         parent_branch_name: BranchName, children_names: List[BranchName]):
    """Complete the fold operation after all commits are applied"""
    global CURRENT_BRANCH

    # Get the updated branches from the stack
    fold_branch = stack.stack.get(fold_branch_name)
    parent_branch = stack.stack[parent_branch_name]

    if not fold_branch:
        # Branch might have been deleted already, just finish up
        cout("✓ Fold operation completed\n", fg="green")
        return

    # Update parent branch commit in stack
    parent_branch.commit = get_commit(parent_branch_name)

    # Reparent children
    for child_name in children_names:
        if child_name in stack.stack:
            child = stack.stack[child_name]
            info("Reparenting {} from {} to {}", child.name, fold_branch.name, parent_branch.name)
            child.parent = parent_branch
            parent_branch.children.add(child)
            fold_branch.children.discard(child)
            set_parent(child.name, parent_branch.name)
            # Update the child's parent commit to the new parent's tip
            set_parent_commit(child.name, parent_branch.commit, child.parent_commit)
            child.parent_commit = parent_branch.commit

    # Remove the folded branch from its parent's children
    parent_branch.children.discard(fold_branch)

    # Delete the branch
    info("Deleting branch {}", fold_branch.name)
    run(CmdArgs(["git", "branch", "-D", fold_branch.name]))

    # Clean up stack parent ref
    run(CmdArgs(["git", "update-ref", "-d", "refs/stack-parent/{}".format(fold_branch.name)]))

    # Remove from stack
    stack.remove(fold_branch.name)

    cout("✓ Successfully folded {} into {}\n", fold_branch.name, parent_branch.name, fg="green")


if __name__ == "__main__":
    main()
