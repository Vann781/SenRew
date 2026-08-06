"""Noticing when you push.

This watches the GIT REPOSITORY, not your terminal. It does not read your
shell history and it does not see what you type.

When you run `git push`, git updates the remote-tracking refs under
.git/refs/remotes/origin. Comparing those between polls is an exact signal
that a push happened - and because it is git state rather than shell state, it
works the same from any shell, from VS Code, and from GitHub Desktop.
"""

import re
import subprocess
from pathlib import Path

GIT_TIMEOUT = 15

# owner/repo out of either remote form:
#   git@github.com:owner/repo.git
#   https://github.com/owner/repo.git
REMOTE_RE = re.compile(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?/?$")


def _git(repo_dir: Path, *args: str) -> str:
    """Run one git command. Never uses a shell, so no quoting surprises."""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:200] or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def is_repo(path: Path) -> bool:
    return (Path(path) / ".git").exists()


def find_repos(paths: list[str]) -> list[Path]:
    """Expand the given paths to git repositories.

    A path that is itself a repo is used directly; otherwise its immediate
    children are checked, so you can point at a folder full of projects.
    """
    found: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            continue
        if is_repo(path):
            found.append(path)
            continue
        found.extend(child for child in sorted(path.iterdir())
                     if child.is_dir() and is_repo(child))
    return found


def remote_slug(repo_dir: Path) -> str | None:
    """'owner/repo' for the origin remote, or None if it is not GitHub."""
    try:
        url = _git(repo_dir, "remote", "get-url", "origin")
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None
    match = REMOTE_RE.search(url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def remote_refs(repo_dir: Path) -> dict[str, str]:
    """Branch -> commit for every origin remote-tracking ref.

    Uses for-each-ref rather than reading .git/refs by hand, because git packs
    refs into .git/packed-refs and a hand-rolled reader misses them.
    """
    try:
        out = _git(repo_dir, "for-each-ref", "--format=%(refname:short) %(objectname)",
                   "refs/remotes/origin")
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return {}

    refs: dict[str, str] = {}
    for line in out.splitlines():
        name, _, sha = line.partition(" ")
        if not sha or name.endswith("/HEAD"):
            continue
        refs[name.removeprefix("origin/")] = sha
    return refs


def detect_pushes(previous: dict[str, str], current: dict[str, str]) -> list[str]:
    """Branches whose commit changed, or that appeared since last time.

    A `git fetch` that pulls someone else's commit also moves these refs, so
    this can fire without you having pushed. That is harmless: the caller then
    checks for an open pull request and whether that exact commit was already
    reviewed, and both of those filter it out.
    """
    return sorted(
        branch for branch, sha in current.items()
        if previous.get(branch) != sha
    )
